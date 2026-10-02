"""Every detector must fire on a case it is meant to catch, and stay quiet otherwise."""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFindings(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Finding = cls.env["mdx.access.finding"]
        cls.Groups = cls.env["res.groups"]
        cls.Access = cls.env["ir.access"]
        cls.model = cls.env["ir.model"]._get("res.partner.category")
        # _get() returns an empty recordset for an unknown model, which would
        # surface much later as a NOT NULL violation on ir_access.model_id
        assert cls.model, "fixture model res.partner.category is missing"
        cls.env.user.group_ids = [
            (4, cls.env.ref("mdx_access_manager.group_access_manager").id)]

    def _findings(self, code):
        return self.Finding.search([("code", "=", code)])

    def test_admin_path_detects_indirect_settings_access(self):
        admin = self.env.ref("base.group_system")
        innocuous = self.Groups.create({
            "name": "MDX Looks Harmless",
            "implied_ids": [(6, 0, admin.ids)],
        })
        self.Finding.action_scan()
        found = self._findings("admin_path").filtered(lambda f: f.group_id == innocuous)
        self.assertTrue(found, "a group implying base.group_system must be reported")
        self.assertIn(found.severity, ("critical", "warning"))

    def test_write_without_read_is_detected(self):
        group = self.Groups.create({"name": "MDX Writer Only"})
        self.Access.search([("model_id", "=", self.model.id),
                            ("group_id", "=", group.id)]).unlink()
        acl = self.Access.create({
            "name": "mdx writer only",
            "model_id": self.model.id,
            "group_id": group.id,
            "operation": "u",
        })
        self.Finding.action_scan()
        found = self._findings("write_without_read").filtered(
            lambda f: f.group_id == group and f.model_id == self.model)
        self.assertTrue(found, "write without read must be reported")
        self.assertTrue(acl.exists())

    def test_write_with_read_is_not_reported(self):
        group = self.Groups.create({"name": "MDX Proper Writer"})
        self.Access.create({
            "name": "mdx proper writer",
            "model_id": self.model.id,
            "group_id": group.id,
            "operation": "ru",
        })
        self.Finding.action_scan()
        found = self._findings("write_without_read").filtered(
            lambda f: f.group_id == group and f.model_id == self.model)
        self.assertFalse(found, "a group that can read must not be flagged")

    def test_redundant_implication_is_detected(self):
        leaf = self.Groups.create({"name": "MDX RI Leaf"})
        middle = self.Groups.create({"name": "MDX RI Middle",
                                     "implied_ids": [(6, 0, leaf.ids)]})
        top = self.Groups.create({
            "name": "MDX RI Top",
            # implies the leaf directly AND through middle - the direct link is redundant
            "implied_ids": [(6, 0, (leaf | middle).ids)],
        })
        self.Finding.action_scan()
        found = self._findings("redundant_implication").filtered(lambda f: f.group_id == top)
        self.assertTrue(found, "an implication reachable another way must be reported")

    def test_read_through_an_implied_group_is_not_reported(self):
        """Read on a base role plus write on the specific group is the normal shape."""
        base = self.Groups.create({"name": "MDX Reader Base"})
        group = self.Groups.create({"name": "MDX Writer On Top",
                                    "implied_ids": [(6, 0, base.ids)]})
        self.Access.create({"name": "mdx base read", "model_id": self.model.id,
                            "group_id": base.id, "operation": "r"})
        self.Access.create({"name": "mdx top write", "model_id": self.model.id,
                            "group_id": group.id, "operation": "u"})
        self.Finding.action_scan()
        found = self._findings("write_without_read").filtered(
            lambda f: f.group_id == group and f.model_id == self.model)
        self.assertFalse(found, "read held through an implied group must not be flagged")

    def test_shadowed_permission_is_detected(self):
        broad = self.Groups.create({"name": "MDX Broad"})
        narrow = self.Groups.create({"name": "MDX Narrow",
                                     "implied_ids": [(6, 0, broad.ids)]})
        self.Access.search([("model_id", "=", self.model.id)]).unlink()
        self.Access.create({"name": "mdx broad read", "model_id": self.model.id,
                            "group_id": broad.id, "operation": "r"})
        shadowed = self.Access.create({
            "name": "mdx narrow read", "model_id": self.model.id,
            "group_id": narrow.id, "operation": "r",
            "domain": "[('active', '=', True)]"})
        useful = self.Access.create({
            "name": "mdx narrow read write", "model_id": self.model.id,
            "group_id": narrow.id, "operation": "ru"})
        self.Finding.action_scan()
        found = self._findings("shadowed_permission").filtered(
            lambda f: f.model_id == self.model)
        self.assertIn(shadowed, found.mapped("access_id"),
                      "a permission fully covered by an implied group's must be reported")
        self.assertNotIn(useful, found.mapped("access_id"),
                         "a permission that adds an operation is not redundant")

    def test_restriction_shadow_is_detected(self):
        group = self.Groups.create({"name": "MDX Rule Group"})
        self.Access.search([("model_id", "=", self.model.id)]).unlink()
        restriction = self.Access.create({
            "name": "mdx restriction narrowing", "model_id": self.model.id,
            "group_id": False, "operation": "crud", "domain": "[('id', '>', 0)]"})
        self.Access.create({
            "name": "mdx group conditional", "model_id": self.model.id,
            "group_id": group.id, "operation": "r", "domain": "[('active', '=', True)]"})
        self.Finding.action_scan()
        found = self._findings("restriction_shadow").filtered(
            lambda f: f.model_id == self.model)
        self.assertEqual(found.access_id, restriction,
                         "a restriction alongside conditional permissions must be reported")

    def test_scan_clears_findings_that_no_longer_apply(self):
        group = self.Groups.create({"name": "MDX Transient Finding"})
        acl = self.Access.create({
            "name": "mdx transient",
            "model_id": self.model.id,
            "group_id": group.id,
            "operation": "u",
        })
        self.Finding.action_scan()
        self.assertTrue(self._findings("write_without_read").filtered(
            lambda f: f.group_id == group))
        acl.operation = "ru"
        self.Finding.action_scan()
        self.assertFalse(
            self._findings("write_without_read").filtered(lambda f: f.group_id == group),
            "a finding that no longer holds must be removed, not left behind",
        )

    def test_acknowledge_survives_a_rescan(self):
        group = self.Groups.create({"name": "MDX Ack Group"})
        self.Access.create({
            "name": "mdx ack",
            "model_id": self.model.id,
            "group_id": group.id,
            "operation": "u",
        })
        self.Finding.action_scan()
        finding = self._findings("write_without_read").filtered(lambda f: f.group_id == group)
        finding.action_acknowledge()
        self.Finding.action_scan()
        finding.invalidate_recordset()
        self.assertEqual(finding.status, "acknowledged",
                         "a rescan must not undo a human's triage")
