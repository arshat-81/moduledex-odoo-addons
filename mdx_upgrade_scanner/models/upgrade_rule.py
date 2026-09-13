"""What the scanner looks for.

Every rule is a record rather than a line of Python, so the catalogue can be
extended, narrowed or switched off from the interface without a patch. The
fields that matter for trust are ``since_version`` and ``source_ref``: each rule
names the Odoo release that deprecated the API and the file and line in the Odoo
source where that is declared, so a finding can always be checked against the
thing it claims.

Severity is deliberately not a matter of taste. It follows from two dates - when
Odoo marked the API, and whether the API is still in the release you are running:

* ``removed``    the API is already gone in the version you are on. It is not a
                 warning about the future, it is broken now.
* ``due``        deprecated two or more releases ago and still present. Odoo's
                 own history puts removal one to two releases after the mark,
                 so this is the set most likely to break on the next upgrade.
* ``deprecated`` marked in the current release. It still works; it will not
                 keep working.
* ``advisory``   worth knowing, not a blocker.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

KIND_SELECTION = [
    ("import", "Python import"),
    ("call", "Python method call"),
    ("attr", "Python attribute"),
    ("kwarg", "Python keyword argument"),
    ("strkey", "Python string, exactly"),
    ("deco_kwarg", "Python decorator argument"),
    ("xml_tag", "XML element"),
    ("xml_attr", "XML attribute"),
    ("xml_field", "XML field name"),
    ("regex", "Regular expression"),
]

SEVERITY_SELECTION = [
    ("removed", "Already removed"),
    ("due", "Due for removal"),
    ("deprecated", "Deprecated"),
    ("advisory", "Advisory"),
]

SEVERITY_ORDER = {"removed": 0, "due": 1, "deprecated": 2, "advisory": 3}


class MdxUpgradeRule(models.Model):
    _name = "mdx.upgrade.rule"
    _description = "Upgrade Scanner Rule"
    _order = "severity, since_version desc, name"

    name = fields.Char(
        required=True, index=True,
        help="The thing being looked for, written the way it appears in code.")
    title = fields.Char(
        required=True,
        help="What a person reading the finding should understand from it.")
    kind = fields.Selection(KIND_SELECTION, required=True, default="call")
    pattern = fields.Char(
        required=True,
        help="What to match. For an import, the dotted module path. For a call "
             "or an attribute, its name. For a keyword argument, the argument "
             "name. For XML, the element or attribute name. For a regular "
             "expression, the expression itself. A string rule matches a "
             "literal equal to the pattern - 'groups_id' as a dict key, not the "
             "word inside a sentence. An XML field rule matches "
             "<field name=\"...\"> with that name. A decorator rule is written "
             "decorator:argument=value, for example route:type=json.")
    severity = fields.Selection(SEVERITY_SELECTION, required=True, default="deprecated",
                                index=True)
    since_version = fields.Char(
        string="Deprecated In",
        help="The Odoo release that marked this deprecated, e.g. 18.0.")
    removed_in = fields.Char(
        help="The release that removed it, where it is already gone.")
    replacement = fields.Char(
        string="Use Instead",
        help="What to write in its place. Shown on every finding.")
    explanation = fields.Text(
        help="Why it changed, where that is worth knowing.")
    source_ref = fields.Char(
        string="Declared In Odoo At",
        help="Where in the Odoo source this deprecation is declared, so the rule "
             "can be verified rather than trusted.")
    applies_to_py = fields.Boolean(string="Scan .py", default=True)
    applies_to_xml = fields.Boolean(string="Scan .xml", default=False)
    active = fields.Boolean(default=True)
    finding_count = fields.Integer(compute="_compute_finding_count",
                                   string="Findings", compute_sudo=True)

    _name_kind_uniq = models.Constraint(
        "UNIQUE(name, kind)",
        "A rule with this name and kind already exists.",
    )

    def _compute_finding_count(self):
        data = self.env["mdx.upgrade.finding"]._read_group(
            [("rule_id", "in", self.ids)], groupby=["rule_id"], aggregates=["__count"])
        counts = {rule.id: count for rule, count in data}
        for rule in self:
            rule.finding_count = counts.get(rule.id, 0)

    @api.constrains("kind", "pattern")
    def _check_pattern(self):
        """A regular expression that does not compile would fail silently in the
        middle of a scan, so it is rejected at the point someone writes it."""
        import re
        for rule in self:
            if rule.kind == "regex":
                try:
                    re.compile(rule.pattern)
                except re.error as error:
                    raise ValidationError(_(
                        "%(name)s is not a valid regular expression: %(error)s",
                        name=rule.name, error=error)) from error

    def action_view_findings(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.title,
            "res_model": "mdx.upgrade.finding",
            "view_mode": "list,form",
            "domain": [("rule_id", "=", self.id)],
        }
