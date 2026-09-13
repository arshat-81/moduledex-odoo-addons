"""The scan itself.

Python is read through the abstract syntax tree rather than searched line by
line. That is the whole difference between a report worth reading and a pile of
false positives: ``check_access_rights`` written in a docstring, a comment, a
translated string or a variable name is not a call, and grep cannot tell the
difference. The tree can, and it also gives an exact line number for free.

XML is parsed for the same reason, with a recovering parser so one malformed
file in a module does not abandon the rest of the scan.
"""

import ast
import logging
import os

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.module import Manifest

_logger = logging.getLogger(__name__)

# Reading every vendored asset would take a long time to find nothing.
# static/ holds Owl component templates, whose attribute names mean something
# entirely different from a view's - an Owl <Dropdown attrs="{...}"/> has nothing
# to do with the attrs that 17.0 removed from views. Views and data files never
# live there, so skipping it removes a whole class of confident nonsense.
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "lib", "i18n", "migrations", "static"}
MAX_BYTES = 2 * 1024 * 1024
MAX_FINDINGS = 20000


class MdxUpgradeScan(models.Model):
    _name = "mdx.upgrade.scan"
    _description = "Upgrade Scan"
    _order = "create_date desc, id desc"
    _rec_name = "display_name"

    scope = fields.Selection(
        [("custom", "Installed custom modules"),
         ("available", "Custom modules in the addons path, installed or not"),
         ("selected", "Selected modules"),
         ("all", "Every installed module, Odoo's included")],
        required=True, default="custom",
        help="Odoo's own addons are excluded by default: they are already "
             "correct for the release you are running, and the point is the code "
             "you have to fix yourself.\n\n"
             "The second option is the one to use before an upgrade. A module "
             "that will not install on the new version cannot be scanned as an "
             "installed module, and that is exactly when you need to know what "
             "is wrong with it.")
    module_ids = fields.Many2many(
        "ir.module.module", string="Modules",
        domain=[("state", "=", "installed")])
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Done")], default="draft", required=True)
    scanned_on = fields.Datetime(readonly=True)
    scanned_by_id = fields.Many2one("res.users", string="Run By", readonly=True)
    module_count = fields.Integer(readonly=True, string="Modules Scanned")
    file_count = fields.Integer(readonly=True, string="Files Read")
    finding_ids = fields.One2many("mdx.upgrade.finding", "scan_id")

    removed_count = fields.Integer(compute="_compute_counts", string="Already Removed")
    due_count = fields.Integer(compute="_compute_counts", string="Due For Removal")
    deprecated_count = fields.Integer(compute="_compute_counts", string="Deprecated")
    advisory_count = fields.Integer(compute="_compute_counts", string="Advisory")
    finding_count = fields.Integer(compute="_compute_counts", string="Findings")
    verdict = fields.Char(compute="_compute_counts")

    @api.depends("finding_ids.severity")
    def _compute_counts(self):
        data = self.env["mdx.upgrade.finding"]._read_group(
            [("scan_id", "in", self.ids)], groupby=["scan_id", "severity"],
            aggregates=["__count"])
        tally = {}
        for scan, severity, count in data:
            tally.setdefault(scan.id, {})[severity] = count
        for scan in self:
            counts = tally.get(scan.id, {})
            scan.removed_count = counts.get("removed", 0)
            scan.due_count = counts.get("due", 0)
            scan.deprecated_count = counts.get("deprecated", 0)
            scan.advisory_count = counts.get("advisory", 0)
            scan.finding_count = sum(counts.values())
            scan.verdict = scan._verdict()

    def _verdict(self):
        """One sentence a person can act on, rather than four numbers."""
        self.ensure_one()
        if self.state != "done":
            return _("Not run yet.")
        if self.removed_count:
            return _("%s call(s) already removed in this version. These are broken now, "
                     "not on upgrade.", self.removed_count)
        if self.due_count:
            return _("%s call(s) in their removal window. This is what the next upgrade "
                     "is most likely to break.", self.due_count)
        if self.deprecated_count:
            return _("Nothing overdue. %s deprecated call(s) to deal with before they "
                     "are removed.", self.deprecated_count)
        return _("Nothing deprecated found. This code is ready.")

    SHORT_SCOPE = {"custom": "Custom modules", "available": "Pre-upgrade",
                   "selected": "Selected modules", "all": "Everything"}

    @api.depends("create_date", "scope")
    def _compute_display_name(self):
        for scan in self:
            when = fields.Datetime.to_string(scan.create_date)[:16] if scan.create_date else _("new")
            scan.display_name = "%s - %s" % (self.SHORT_SCOPE.get(scan.scope, ""), when)

    # ------------------------------------------------------------------
    # locating what to scan
    # ------------------------------------------------------------------
    @api.model
    def _core_roots(self):
        """The directories Odoo ships its own addons in.

        Found by asking where ``base`` and ``web`` actually live rather than by
        assuming a layout, because a packaged install, a source checkout and
        Odoo.sh all put them somewhere slightly different.
        """
        roots = set()
        for known in ("base", "web"):
            manifest = Manifest.for_addon(known, display_warning=False)
            if manifest and manifest.path:
                roots.add(os.path.dirname(os.path.realpath(manifest.path)))
        return roots

    def _modules_to_scan(self):
        self.ensure_one()
        Module = self.env["ir.module.module"]
        if self.scope == "selected":
            modules = self.module_ids
            if not modules:
                raise UserError(_("Choose at least one module, or change the scope."))
        elif self.scope == "available":
            # Everything Odoo can see on disk, whether or not it loaded.
            modules = Module.search([("state", "!=", "uninstallable")])
        else:
            modules = Module.search([("state", "=", "installed")])

        core_roots = self._core_roots()
        out = []
        for module in modules:
            manifest = Manifest.for_addon(module.name, display_warning=False)
            path = manifest.path if manifest else None
            if not path or not os.path.isdir(path):
                continue
            is_core = os.path.dirname(os.path.realpath(path)) in core_roots
            if self.scope in ("custom", "available") and is_core:
                continue
            if module.name == "mdx_upgrade_scanner":
                # The rule catalogue necessarily contains every pattern the
                # scanner hunts for, so scanning it reports the rules themselves.
                continue
            out.append((module.name, os.path.realpath(path)))
        return out

    # ------------------------------------------------------------------
    # running
    # ------------------------------------------------------------------
    def action_scan(self):
        self.ensure_one()
        rules = self.env["mdx.upgrade.rule"].search([])
        if not rules:
            raise UserError(_("There are no active rules to scan with."))

        self.finding_ids.unlink()
        targets = self._modules_to_scan()
        if not targets:
            raise UserError(_(
                "No modules matched. With a custom-only scope this means every "
                "module found ships with Odoo, so there is nothing of yours to check."))

        py_rules = rules.filtered(lambda r: r.applies_to_py)
        xml_rules = rules.filtered(lambda r: r.applies_to_xml)
        findings, files = [], 0

        for module_name, path in targets:
            for file_path in self._walk(path):
                if len(findings) >= MAX_FINDINGS:
                    break
                rel = os.path.relpath(file_path, path)
                text = self._read(file_path)
                if text is None:
                    continue
                files += 1
                if file_path.endswith(".py"):
                    hits = self._scan_python(text, py_rules)
                else:
                    hits = self._scan_xml(text, xml_rules)
                lines = text.splitlines()
                for rule, line in hits:
                    snippet = lines[line - 1].strip()[:200] if 0 < line <= len(lines) else ""
                    findings.append({
                        "scan_id": self.id, "rule_id": rule.id, "rule_title": rule.title,
                        "severity": rule.severity, "module_name": module_name,
                        "file_path": rel, "line": line, "snippet": snippet,
                        "replacement": rule.replacement,
                        "since_version": rule.since_version,
                        "source_ref": rule.source_ref,
                    })

        self.env["mdx.upgrade.finding"].create(findings)
        self.write({
            "state": "done",
            "scanned_on": fields.Datetime.now(),
            "scanned_by_id": self.env.user.id,
            "module_count": len(targets),
            "file_count": files,
        })
        return True

    def _walk(self, path):
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith((".py", ".xml")):
                    yield os.path.join(dirpath, name)

    def _read(self, file_path):
        try:
            if os.path.getsize(file_path) > MAX_BYTES:
                return None
            with open(file_path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    # ------------------------------------------------------------------
    # the two readers
    # ------------------------------------------------------------------
    def _scan_python(self, text, rules):
        """Match against the parsed tree, so only real code counts."""
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # Written for a Python this server cannot parse, or simply broken.
            # Either way, guessing with a regular expression would be worse.
            return []

        by_kind = {}
        for rule in rules:
            by_kind.setdefault(rule.kind, []).append(rule)

        hits = []
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(id(node.func))

        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A decorator argument, which is the only place some of these
                # values mean anything. type='json' inside dict(type='json'), or
                # an assignment to routing_type, is not a route - and a regular
                # expression cannot tell, which is how a scanner loses trust.
                for rule in by_kind.get("deco_kwarg", []):
                    if self._decorator_matches(node, rule.pattern):
                        hits.append((rule, line))

            if isinstance(node, ast.Import):
                for alias in node.names:
                    for rule in by_kind.get("import", []):
                        if self._module_matches(alias.name, rule.pattern):
                            hits.append((rule, line))
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                for rule in by_kind.get("import", []):
                    if self._module_matches(base, rule.pattern):
                        hits.append((rule, line))
                        continue
                    # from odoo.osv import expression -> odoo.osv.expression
                    for alias in node.names:
                        full = "%s.%s" % (base, alias.name) if base else alias.name
                        if self._module_matches(full, rule.pattern):
                            hits.append((rule, line))

            elif isinstance(node, ast.Call):
                name = None
                qualifier = None
                if isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                    qualifier = self._qualifier(node.func.value)
                elif isinstance(node.func, ast.Name):
                    name = node.func.id
                for rule in by_kind.get("call", []):
                    if self._call_matches(rule.pattern, name, qualifier):
                        hits.append((rule, line))
                for rule in by_kind.get("kwarg", []):
                    if any(kw.arg == rule.pattern for kw in node.keywords):
                        hits.append((rule, line))

            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # An exact string, which is how a field name travels through
                # Python: {"groups_id": ...}, ["force_company"], mapped("x").
                # Equality, not containment - otherwise a sentence that merely
                # mentions the name becomes a finding, which is the very thing
                # this scanner exists not to do.
                for rule in by_kind.get("strkey", []):
                    if node.value == rule.pattern:
                        hits.append((rule, line))

            elif isinstance(node, ast.Attribute):
                # An attribute that is being called is reported by the call rule,
                # so reporting it here as well would count the same code twice.
                if id(node) in called:
                    continue
                for rule in by_kind.get("attr", []):
                    if node.attr == rule.pattern:
                        hits.append((rule, line))

        for rule in by_kind.get("regex", []):
            hits.extend(self._scan_regex(text, rule))
        return hits

    def _scan_regex(self, text, rule):
        import re
        try:
            expression = re.compile(rule.pattern)
        except re.error:
            return []
        return [(rule, text.count("\n", 0, match.start()) + 1)
                for match in expression.finditer(text)]

    @classmethod
    def _decorator_matches(cls, func_node, pattern):
        """``route:type=json`` - a decorator called route, carrying type="json"."""
        deco_name, _sep, rest = pattern.partition(":")
        arg_name, _eq, wanted = rest.partition("=")
        if not (deco_name and arg_name):
            return False
        for decorator in func_node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            name = getattr(decorator.func, "attr", None) or getattr(decorator.func, "id", None)
            if name != deco_name:
                continue
            for keyword in decorator.keywords:
                if keyword.arg != arg_name:
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and str(value.value) == wanted:
                    return True
        return False

    @staticmethod
    def _qualifier(node):
        """The thing a method was called on, where it has a simple name."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    @staticmethod
    def _call_matches(pattern, name, qualifier):
        """``expression.AND`` must not match ``Domain.AND``.

        Domain.AND is the replacement, so matching on the method name alone
        would report correct, current code as deprecated and tell the reader to
        rewrite it into what it already is. A dotted pattern therefore pins the
        object as well as the method; a bare pattern still matches any caller.
        """
        if "." in pattern:
            want_qualifier, _sep, want_name = pattern.rpartition(".")
            return name == want_name and qualifier == want_qualifier
        return name == pattern

    @staticmethod
    def _module_matches(dotted, pattern):
        """``odoo.osv`` matches ``odoo.osv`` and ``odoo.osv.expression``, but not
        ``odoo.osvalue``."""
        return dotted == pattern or dotted.startswith(pattern + ".")

    def _scan_xml(self, text, rules):
        parser = etree.XMLParser(recover=True, resolve_entities=False)
        try:
            root = etree.fromstring(text.encode("utf-8"), parser=parser)
        except etree.XMLSyntaxError:
            return []
        if root is None:
            return []

        tag_rules = [r for r in rules if r.kind == "xml_tag"]
        attr_rules = [r for r in rules if r.kind == "xml_attr"]
        field_rules = [r for r in rules if r.kind == "xml_field"]
        hits = []
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            line = element.sourceline or 0
            for rule in tag_rules:
                if element.tag == rule.pattern:
                    hits.append((rule, line))
            for rule in attr_rules:
                if rule.pattern in element.attrib:
                    hits.append((rule, line))
            if element.tag == "field":
                name = element.get("name")
                for rule in field_rules:
                    if name == rule.pattern:
                        hits.append((rule, line))

        for rule in rules:
            if rule.kind == "regex":
                hits.extend(self._scan_regex(text, rule))
        return hits

    # ------------------------------------------------------------------
    def action_view_findings(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Findings"),
            "res_model": "mdx.upgrade.finding",
            "view_mode": "list,form",
            "domain": [("scan_id", "=", self.id)],
        }
