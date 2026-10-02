"""The simulator reports what the server would do, so every verdict is checked against it."""

from odoo.tests import TransactionCase, tagged

OPERATIONS = ("read", "write", "create", "unlink")


@tagged("post_install", "-at_install")
class TestSecuritySimulator(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Access = cls.env["ir.access"]
        cls.model = cls.env["ir.model"]._get("res.partner.category")
        assert cls.model, "fixture model res.partner.category is missing"
        cls.group = cls.env["res.groups"].create({"name": "MDX Sim Group"})
        cls.other_group = cls.env["res.groups"].create({"name": "MDX Sim Other"})
        cls.user = cls.env["res.users"].create({
            "name": "MDX Sim Subject", "login": "mdx_sim_subject",
            "group_ids": [(6, 0, cls.group.ids)],
        })
        Category = cls.env["res.partner.category"]
        cls.parent = Category.create({"name": "MDX Parent"})
        cls.yes = Category.create({"name": "MDX Yes", "parent_id": cls.parent.id})
        cls.no = Category.create({"name": "MDX No"})
        cls.Access.with_context(active_test=False).search(
            [("model_id", "=", cls.model.id)]).unlink()

    def _grant(self, group, operation, domain=False, **extra):
        return self.Access.create(dict({
            "name": "mdx sim %s %s" % (group.name if group else "restriction", operation),
            "model_id": self.model.id,
            "group_id": group.id if group else False,
            "operation": operation,
            "domain": domain,
        }, **extra))

    def _simulate(self, record=None):
        simulation = self.env["mdx.security.simulator"].create({
            "user_id": self.user.id,
            "model_id": self.model.id,
            "record_id": record.id if record else 0,
        })
        simulation.action_simulate()
        return simulation

    def _line(self, simulation, operation):
        return simulation.operation_line_ids.filtered(lambda l: l.operation == operation)

    # ------------------------------------------------------------------
    def test_no_permission_blocks_everything(self):
        simulation = self._simulate()
        self.assertEqual(set(simulation.operation_line_ids.mapped("status")), {"blocked_acl"})
        self.assertFalse(any(simulation.operation_line_ids.mapped("final_allowed")))

    def test_a_restriction_alone_grants_nothing(self):
        """Since Odoo 20 a rule without a group narrows; it no longer grants."""
        self._grant(False, "crud")
        simulation = self._simulate()
        self.assertEqual(set(simulation.operation_line_ids.mapped("status")), {"blocked_acl"})
        self.assertFalse(simulation.access_line_ids, "a restriction is not a permission")
        self.assertTrue(simulation.rule_line_ids.restriction)

    def test_permission_grants_only_its_operations(self):
        self._grant(self.group, "cr")
        simulation = self._simulate()
        self.assertTrue(self._line(simulation, "read").acl_allowed)
        self.assertEqual(self._line(simulation, "read").status, "conditional")
        self.assertEqual(self._line(simulation, "create").status, "allowed")
        self.assertEqual(self._line(simulation, "write").status, "blocked_acl")
        self.assertEqual(self._line(simulation, "unlink").status, "blocked_acl")
        access_line = simulation.access_line_ids
        self.assertTrue(access_line.applies_to_user)
        self.assertEqual(
            (access_line.perm_read, access_line.perm_write, access_line.perm_create,
             access_line.perm_unlink), (True, False, True, False))

    def test_permission_of_another_group_does_not_apply(self):
        self._grant(self.other_group, "crud")
        simulation = self._simulate()
        self.assertFalse(simulation.access_line_ids.applies_to_user)
        self.assertEqual(self._line(simulation, "read").status, "blocked_acl")

    def test_conditional_permission_is_checked_on_the_record(self):
        self._grant(self.group, "r", domain="[('name', '=', 'MDX Yes')]")
        allowed = self._simulate(self.yes)
        self.assertEqual(self._line(allowed, "read").status, "allowed")
        self.assertEqual(allowed.rule_line_ids.result_read, "pass")
        self.assertFalse(allowed.rule_line_ids.restriction)

        blocked = self._simulate(self.no)
        self.assertEqual(self._line(blocked, "read").status, "blocked_rule")
        self.assertEqual(blocked.rule_line_ids.result_read, "fail")
        self.assertEqual(blocked.rule_line_ids.result_write, "not_applicable")

    def test_restriction_narrows_a_permission(self):
        self._grant(self.group, "r")
        self._grant(False, "r", domain="[('name', '!=', 'MDX No')]")
        blocked = self._simulate(self.no)
        self.assertEqual(self._line(blocked, "read").status, "blocked_rule")
        rule_line = blocked.rule_line_ids
        self.assertTrue(rule_line.restriction)
        self.assertTrue(rule_line.applies_to_user, "a restriction applies to every user")
        self.assertEqual(rule_line.result_read, "fail")
        self.assertEqual(self._line(self._simulate(self.yes), "read").status, "allowed")

    def test_access_operator_is_resolved_as_the_simulated_user(self):
        """('parent_id', 'access', 'write') needs write on the parent."""
        self._grant(self.group, "r", domain="[('parent_id', 'access', 'write')]")
        self.assertEqual(
            self._line(self._simulate(self.yes), "read").status, "blocked_acl",
            "read is delegated to write, which the user was never granted")
        self._grant(self.group, "u")
        with_parent = self._simulate(self.yes)
        self.assertEqual(self._line(with_parent, "read").status, "allowed")
        delegated = with_parent.rule_line_ids.filtered(lambda l: "access" in l.domain_text)
        self.assertEqual(delegated.result_read, "pass")
        without_parent = self._simulate(self.no)
        self.assertEqual(self._line(without_parent, "read").status, "blocked_rule",
                         "a record with no parent cannot satisfy the delegated condition")

    def test_verdicts_match_the_server(self):
        self._grant(self.group, "ru", domain="[('name', 'like', 'MDX')]")
        self._grant(self.group, "c")
        self._grant(False, "u", domain="[('name', '!=', 'MDX No')]")
        for record in (self.yes, self.no, self.parent):
            simulation = self._simulate(record)
            as_user = record.with_user(self.user)
            for operation in OPERATIONS:
                line = self._line(simulation, operation)
                self.assertEqual(
                    line.final_allowed, as_user.has_access(operation),
                    "%s on %s must match has_access" % (operation, record.name))
                self.assertEqual(
                    line.acl_allowed, as_user.browse().has_access(operation),
                    "model-level %s must match has_access on the empty recordset" % operation)

    def test_security_domain_is_the_one_the_server_enforces(self):
        self._grant(self.group, "r", domain="[('name', '=', 'MDX Yes')]")
        simulation = self._simulate()
        expected = repr(list(
            self.env["res.partner.category"].with_user(self.user)._access_domain("read")))
        self.assertEqual(self._line(simulation, "read").rule_domain, expected)
        self.assertEqual(self._line(simulation, "read").visible_count, 1)

    def test_shortcuts_open_the_native_access_model(self):
        simulation = self._simulate()
        self.assertEqual(simulation.action_open_native_access()["res_model"], "ir.access")
        self.assertEqual(simulation.action_open_native_rules()["res_model"], "ir.access")
