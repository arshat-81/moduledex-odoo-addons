"""The resolution engine must agree with the server, not approximate it."""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAccessEngine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Engine = cls.env["mdx.access.engine"]
        cls.Groups = cls.env["res.groups"]
        cls.Access = cls.env["ir.model.access"]
        # A model that certainly exists and that no test will mutate.
        cls.model = cls.env["ir.model"]._get("res.partner.category")
        # _get() returns an empty recordset for an unknown model, which would
        # surface much later as a NOT NULL violation on ir_model_access.model_id
        assert cls.model, "fixture model res.partner.category is missing"

        cls.leaf = cls.Groups.create({"name": "MDX Test Leaf"})
        cls.middle = cls.Groups.create({"name": "MDX Test Middle",
                                        "implied_ids": [(6, 0, cls.leaf.ids)]})
        cls.top = cls.Groups.create({"name": "MDX Test Top",
                                     "implied_ids": [(6, 0, cls.middle.ids)]})

    def test_expand_groups_is_transitive_and_reflexive(self):
        expanded = self.Engine._expand_groups(self.top)
        self.assertIn(self.top, expanded, "the closure must include the group itself")
        self.assertIn(self.middle, expanded)
        self.assertIn(self.leaf, expanded, "implication must be followed transitively")

    def test_expand_groups_accepts_ids(self):
        self.assertEqual(
            self.Engine._expand_groups(self.top.ids),
            self.Engine._expand_groups(self.top),
        )

    def test_expand_groups_empty(self):
        self.assertFalse(self.Engine._expand_groups([]))

    def test_model_without_acl_is_denied(self):
        """Odoo denies a model that has no access control row at all."""
        self.Access.search([("model_id", "=", self.model.id)]).unlink()
        perms = self.Engine._perms_by_model(self.top.ids, model_names=[self.model.model])
        self.assertNotIn(self.model.model, perms,
                         "a model with no ACL must not appear as granted")

    def test_group_acl_grants_only_its_modes(self):
        self.Access.search([("model_id", "=", self.model.id)]).unlink()
        self.Access.create({
            "name": "mdx test read only",
            "model_id": self.model.id,
            "group_id": self.leaf.id,
            "perm_read": True,
        })
        perms = self.Engine._perms_by_model(
            self.Engine._expand_groups(self.top).ids, model_names=[self.model.model])
        self.assertTrue(perms[self.model.model]["read"])
        self.assertFalse(perms[self.model.model]["write"])
        self.assertFalse(perms[self.model.model]["create"])
        self.assertFalse(perms[self.model.model]["unlink"])

    def test_access_is_inherited_through_implication(self):
        """The grant sits on the leaf; the user holds only the top group."""
        self.Access.search([("model_id", "=", self.model.id)]).unlink()
        self.Access.create({
            "name": "mdx test leaf grant",
            "model_id": self.model.id,
            "group_id": self.leaf.id,
            "perm_read": True,
        })
        direct = self.Engine._perms_by_model(self.top.ids, model_names=[self.model.model])
        self.assertNotIn(self.model.model, direct,
                         "resolving the unexpanded set must not see the leaf's grant")
        expanded = self.Engine._perms_by_model(
            self.Engine._expand_groups(self.top).ids, model_names=[self.model.model])
        self.assertTrue(expanded[self.model.model]["read"],
                        "expanding the group set must pick up the implied group's grant")

    def test_global_acl_grants_to_a_user_with_no_groups(self):
        """A NULL group_id is a grant to everyone and must survive an empty set."""
        self.Access.search([("model_id", "=", self.model.id)]).unlink()
        self.Access.create({
            "name": "mdx test global",
            "model_id": self.model.id,
            "group_id": False,
            "perm_read": True,
        })
        perms = self.Engine._perms_by_model([], model_names=[self.model.model])
        self.assertTrue(perms[self.model.model]["read"])

    def test_inactive_acl_grants_nothing(self):
        self.Access.search([("model_id", "=", self.model.id)]).unlink()
        self.Access.create({
            "name": "mdx test archived",
            "model_id": self.model.id,
            "group_id": self.leaf.id,
            "perm_read": True,
            "active": False,
        })
        perms = self.Engine._perms_by_model(self.leaf.ids, model_names=[self.model.model])
        self.assertNotIn(self.model.model, perms)

    def test_engine_matches_the_server(self):
        """The whole point: agree with ir.model.access for a real user."""
        user = self.env["res.users"].create({
            "name": "MDX Engine Probe",
            "login": "mdx_engine_probe",
            "group_ids": [(6, 0, self.top.ids)],
        })
        _explicit, effective = self.Engine._user_group_sets(user)
        mine = self.Engine._perms_by_model(effective.ids)
        theirs = self.env["ir.model.access"].with_user(user)._get_allowed_models("read")
        self.assertEqual(
            {m for m, p in mine.items() if p["read"]},
            set(theirs),
            "the engine's readable models must match the server's own list exactly",
        )

    def test_rules_split_global_and_group(self):
        rule_model = self.env["ir.rule"]
        rule_model.search([("model_id", "=", self.model.id)]).unlink()
        global_rule = rule_model.create({
            "name": "mdx test global rule",
            "model_id": self.model.id,
            "domain_force": "[(1, '=', 1)]",
        })
        group_rule = rule_model.create({
            "name": "mdx test group rule",
            "model_id": self.model.id,
            "domain_force": "[(1, '=', 1)]",
            "groups": [(6, 0, self.leaf.ids)],
        })
        globals_, groups_ = self.Engine._rules_for(self.model.model, self.leaf.ids, "read")
        self.assertIn(global_rule, globals_)
        self.assertIn(group_rule, groups_)
        self.assertNotIn(group_rule, globals_)

        # a rule whose groups the user does not hold is not applied at all
        _globals2, groups2 = self.Engine._rules_for(self.model.model, [], "read")
        self.assertNotIn(group_rule, groups2)

    def test_group_label_is_privilege_qualified(self):
        privilege = self.env["res.groups.privilege"].create({"name": "MDX Test Privilege"})
        group = self.Groups.create({"name": "Manager", "privilege_id": privilege.id})
        self.assertEqual(self.Engine._group_label(group), "MDX Test Privilege / Manager")

    def test_implication_path_explains_inheritance(self):
        paths = self.Engine._implication_paths(self.top, self.leaf)
        self.assertTrue(paths)
        self.assertIn("MDX Test Top", paths[0])
        self.assertIn("MDX Test Leaf", paths[0])
