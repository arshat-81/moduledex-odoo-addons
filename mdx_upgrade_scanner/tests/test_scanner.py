"""Proving the scanner reads code rather than searching it.

The interesting tests are the negative ones. Anybody can match a string; the
value of this module is that it does not report ``check_access_rights`` when the
words appear in a comment, a docstring, a string literal or somebody's variable
name, because a report full of those is a report nobody finishes reading.
"""

import os
import tempfile
import textwrap

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestScanner(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Scan = cls.env["mdx.upgrade.scan"]
        cls.Rule = cls.env["mdx.upgrade.rule"]

    # ------------------------------------------------------------------
    def scan_source(self, source, suffix=".py"):
        """Run the readers directly over a snippet, returning rule names hit."""
        rules = self.Rule.search([])
        scan = self.Scan.new({"scope": "custom"})
        if suffix == ".py":
            hits = scan._scan_python(source, rules.filtered("applies_to_py"))
        else:
            hits = scan._scan_xml(source, rules.filtered("applies_to_xml"))
        return [rule.name for rule, _line in hits]

    # ------------------------------------------------------------------
    # it finds real code
    # ------------------------------------------------------------------
    def test_finds_a_deprecated_call(self):
        found = self.scan_source("def f(self):\n    self.check_access_rights('read')\n")
        self.assertIn("check_access_rights", found)

    def test_finds_a_deprecated_attribute(self):
        found = self.scan_source("def f(self):\n    return self._cr.fetchall()\n")
        self.assertIn("self._cr", found)

    def test_finds_a_deprecated_keyword_argument(self):
        found = self.scan_source(
            "total = fields.Float(string='Total', group_operator='sum')\n")
        self.assertIn("group_operator=", found)

    def test_finds_a_deprecated_import(self):
        found = self.scan_source("from odoo.osv import expression\n")
        self.assertIn("odoo.osv", found)

    def test_finds_a_deprecated_import_of_the_package_itself(self):
        found = self.scan_source("import odoo.osv.expression\n")
        self.assertIn("odoo.osv", found)

    def test_reports_the_right_line(self):
        source = "import os\n\n\ndef f(self):\n    self.check_access_rights('read')\n"
        rules = self.Rule.search([("name", "=", "check_access_rights")])
        hits = self.Scan.new({"scope": "custom"})._scan_python(source, rules)
        self.assertEqual([line for _rule, line in hits], [5],
                         "a finding nobody can locate is not worth reporting")

    # ------------------------------------------------------------------
    # it does not find things that are not code
    # ------------------------------------------------------------------
    def test_a_comment_is_not_a_finding(self):
        found = self.scan_source("# call check_access_rights here one day\nx = 1\n")
        self.assertNotIn("check_access_rights", found,
                         "grep would report this; parsing the code does not")

    def test_a_docstring_is_not_a_finding(self):
        found = self.scan_source('def f():\n    """Do not use check_access_rights."""\n    return 1\n')
        self.assertNotIn("check_access_rights", found)

    def test_a_string_literal_is_not_a_finding(self):
        found = self.scan_source("message = 'check_access_rights is deprecated'\n")
        self.assertNotIn("check_access_rights", found)

    def test_a_similarly_named_variable_is_not_a_finding(self):
        found = self.scan_source("check_access_rights_done = True\n")
        self.assertNotIn("check_access_rights", found)

    def test_a_similar_module_name_is_not_a_finding(self):
        """odoo.osv must not match odoo.osvalue."""
        self.assertFalse(self.Scan._module_matches("odoo.osvalue", "odoo.osv"))
        self.assertTrue(self.Scan._module_matches("odoo.osv", "odoo.osv"))
        self.assertTrue(self.Scan._module_matches("odoo.osv.expression", "odoo.osv"))

    def test_a_called_attribute_is_reported_once(self):
        """_cr is an attribute rule and read_group a call rule; a call must not
        be counted by both."""
        source = "def f(self):\n    return self.env['x'].read_group([], [], [])\n"
        found = self.scan_source(source)
        self.assertEqual(found.count("read_group"), 1)

    def test_unparseable_python_is_skipped_quietly(self):
        found = self.scan_source("def broken(:\n  this is not python\n")
        self.assertEqual(found, [],
                         "a file we cannot parse must not be guessed at")

    def test_an_exact_string_is_a_finding(self):
        """A field name travels through Python as a string, so it has to count."""
        found = self.scan_source("vals = {'groups_id': [(6, 0, ids)]}\n")
        self.assertIn('"groups_id"', found)

    def test_the_same_word_inside_a_sentence_is_not(self):
        """The case that made this rule kind necessary.

        A migration tool's own documentation says the word groups_id constantly.
        Matching the string exactly separates a field name from prose about one.
        """
        source = (
            'MESSAGE = _("groups_id field was renamed to group_ids across the framework")\n'
            'PATTERN = r"<field\\s+name=[\'\"]groups_id[\'\"]"\n')
        found = self.scan_source(source)
        self.assertNotIn('"groups_id"', found,
                         "prose about a field name is not a use of it")

    def test_a_context_key_in_a_list_is_a_finding(self):
        found = self.scan_source(
            'KEYS = ("tz", "lang", "force_company", "active_test")\n')
        self.assertIn("force_company", found)

    def test_an_xml_field_name_is_matched_by_name(self):
        found = self.scan_source(
            "<odoo><record model='res.users'><field name='groups_id' eval='[(6,0,[])]'/>"
            "</record></odoo>", suffix=".xml")
        self.assertIn('<field name="groups_id">', found)

    def test_a_renamed_xml_field_is_clean(self):
        found = self.scan_source(
            "<odoo><record model='res.users'><field name='group_ids' eval='[(6,0,[])]'/>"
            "</record></odoo>", suffix=".xml")
        self.assertNotIn('<field name="groups_id">', found)

    # ------------------------------------------------------------------
    # xml
    # ------------------------------------------------------------------
    def test_finds_a_tree_view(self):
        found = self.scan_source(
            "<odoo><record><field name='arch' type='xml'><tree><field name='x'/></tree>"
            "</field></record></odoo>", suffix=".xml")
        self.assertIn("<tree>", found)

    def test_finds_attrs(self):
        found = self.scan_source(
            "<form><field name='x' attrs=\"{'invisible': [('y','=',1)]}\"/></form>",
            suffix=".xml")
        self.assertIn("attrs=", found)

    def test_a_list_view_is_clean(self):
        found = self.scan_source(
            "<odoo><list><field name='x'/></list></odoo>", suffix=".xml")
        self.assertNotIn("<tree>", found)

    def test_malformed_xml_does_not_abandon_the_scan(self):
        found = self.scan_source("<form><field name='x'</form>", suffix=".xml")
        self.assertIsInstance(found, list)

    # ------------------------------------------------------------------
    # telling the old API from the new one
    # ------------------------------------------------------------------
    def test_the_deprecated_helper_is_found(self):
        found = self.scan_source(
            "from odoo.osv import expression\n"
            "d = expression.AND([a, b])\n")
        self.assertIn("expression.AND", found)

    def test_the_replacement_is_not_reported_as_the_thing_it_replaces(self):
        """Domain.AND is the fix, not the problem.

        Matching on the method name alone would tell somebody who has already
        migrated to rewrite correct code into itself, which is the fastest way
        to make a report untrustworthy.
        """
        found = self.scan_source(
            "from odoo.fields import Domain\n"
            "d = Domain.AND([a, b])\n")
        self.assertNotIn("expression.AND", found)
        self.assertNotIn("expression.OR", found)

    def test_a_bare_pattern_still_matches_any_caller(self):
        found = self.scan_source("self.env['x'].check_access_rights('read')\n")
        self.assertIn("check_access_rights", found)

    def test_owl_templates_are_not_walked(self):
        """attrs on an Owl component is not the attrs that 17.0 removed, and
        every Odoo addon ships templates using it - so static/ is not walked."""
        import shutil
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "static", "src"))
        os.makedirs(os.path.join(root, "views"))
        with open(os.path.join(root, "static", "src", "widget.xml"), "w") as handle:
            handle.write("<templates><Dropdown attrs=\"{a:1}\"/></templates>")
        with open(os.path.join(root, "views", "real_view.xml"), "w") as handle:
            handle.write("<odoo><form><field name='x' attrs=\"{}\"/></form></odoo>")

        walked = [os.path.relpath(p, root) for p in self.Scan._walk(root)]
        self.assertIn(os.path.join("views", "real_view.xml"), walked)
        self.assertNotIn(os.path.join("static", "src", "widget.xml"), walked,
                         "an Owl template must not be read as a view")

    def test_a_json_route_is_found(self):
        found = self.scan_source(
            "class C:\n"
            "    @http.route('/x', type='json', auth='user')\n"
            "    def x(self):\n        return 1\n")
        self.assertIn("@route(type='json')", found)

    def test_a_jsonrpc_route_is_clean(self):
        found = self.scan_source(
            "class C:\n"
            "    @http.route('/x', type='jsonrpc', auth='user')\n"
            "    def x(self):\n        return 1\n")
        self.assertNotIn("@route(type='json')", found)

    def test_type_json_elsewhere_is_not_a_route(self):
        """Both of these were reported by a regular expression, in Odoo's own
        source, and neither is a route."""
        found = self.scan_source(
            "spec = dict(type='json', string='Json')\n"
            "totp_hook.routing_type = 'json'\n")
        self.assertNotIn("@route(type='json')", found)

    def test_the_domain_operator_rule_needs_a_domain(self):
        found = self.scan_source("code = compile(expression, '<>', 'exec')\n")
        self.assertNotIn("<> and == in domains", found,
                         "a filename placeholder is not a domain operator")

    def test_the_domain_operator_rule_finds_a_real_domain(self):
        found = self.scan_source("domain = [('name', '<>', 'x')]\n")
        self.assertIn("<> and == in domains", found)

    # ------------------------------------------------------------------
    # severity, which is the product's actual claim
    # ------------------------------------------------------------------
    def test_severity_reflects_the_removal_window(self):
        """Marked in 18.0 and still present in 19.0 is 'due'; marked in 19.0 is
        merely 'deprecated'. That distinction is the reason to run this."""
        due = self.env.ref("mdx_upgrade_scanner.rule_check_access_rights")
        self.assertEqual(due.severity, "due")
        self.assertEqual(due.since_version, "18.0")
        recent = self.env.ref("mdx_upgrade_scanner.rule_toggle_active")
        self.assertEqual(recent.severity, "deprecated")
        self.assertEqual(recent.since_version, "19.0")

    def test_every_rule_cites_its_source(self):
        """A rule nobody can verify is a rule nobody should trust."""
        unsourced = self.Rule.search([("source_ref", "in", (False, ""))])
        self.assertFalse(unsourced.mapped("name"),
                         "every rule must say where in Odoo the deprecation is declared")

    def test_uninstalled_modules_can_be_scanned_before_the_upgrade(self):
        """The scope that matters when preparing an upgrade.

        A module that will not install on the new version cannot be scanned as
        an installed module, which is precisely when its problems need finding.
        """
        scan = self.Scan.create({"scope": "available"})
        installed = self.Scan.create({"scope": "custom"})
        self.assertGreaterEqual(len(scan._modules_to_scan()),
                                len(installed._modules_to_scan()),
                                "the pre-upgrade scope must not see fewer modules")

    def test_the_scanner_does_not_report_its_own_rules(self):
        """Its catalogue contains every pattern it looks for, by definition."""
        names = [name for name, _path in self.Scan.create(
            {"scope": "available"})._modules_to_scan()]
        self.assertNotIn("mdx_upgrade_scanner", names)

    def test_a_bad_regex_is_refused_when_written(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.Rule.create({"name": "bad", "title": "bad", "kind": "regex",
                              "pattern": "([unclosed", "severity": "advisory"})

    # ------------------------------------------------------------------
    # the whole run
    # ------------------------------------------------------------------
    def test_a_scan_over_a_real_directory_reports_what_is_there(self):
        """End to end, against a throwaway module on disk."""
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root, True)
        os.makedirs(os.path.join(root, "models"))
        with open(os.path.join(root, "models", "thing.py"), "w") as handle:
            handle.write(textwrap.dedent("""\
                from odoo.osv import expression

                class Thing:
                    def go(self):
                        # check_access_rights in a comment does not count
                        self.check_access_rights('read')
                        return self._cr
            """))
        with open(os.path.join(root, "views.xml"), "w") as handle:
            handle.write("<odoo><tree><field name='x'/></tree></odoo>")

        scan = self.Scan.create({"scope": "custom"})
        rules = self.Rule.search([])
        findings = []
        for path in scan._walk(root):
            text = scan._read(path)
            hits = (scan._scan_python(text, rules.filtered("applies_to_py"))
                    if path.endswith(".py")
                    else scan._scan_xml(text, rules.filtered("applies_to_xml")))
            findings.extend(rule.name for rule, _line in hits)

        self.assertIn("odoo.osv", findings)
        self.assertIn("check_access_rights", findings)
        self.assertIn("self._cr", findings)
        self.assertIn("<tree>", findings)
        self.assertEqual(findings.count("check_access_rights"), 1,
                         "the comment must not have produced a second finding")

    def test_verdict_leads_with_what_is_already_broken(self):
        scan = self.Scan.create({"scope": "custom"})
        scan.state = "done"
        self.env["mdx.upgrade.finding"].create([
            {"scan_id": scan.id, "severity": "removed", "rule_title": "x"},
            {"scan_id": scan.id, "severity": "due", "rule_title": "y"},
        ])
        scan.invalidate_recordset()
        self.assertIn("broken now", scan.verdict,
                      "an already-removed call outranks anything merely due")

    def test_a_clean_scan_says_so_plainly(self):
        scan = self.Scan.create({"scope": "custom"})
        scan.state = "done"
        scan.invalidate_recordset()
        self.assertIn("ready", scan.verdict)
