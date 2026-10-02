"""The resolution engine must agree with the server, not approximate it."""

from odoo.tests import TransactionCase, tagged

MODES = ("read", "write", "create", "unlink")


@tagged("post_install", "-at_install")
class TestAccessEngine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Engine = cls.env["mdx.access.engine"]
        cls.Groups = cls.env["res.groups"]
        cls.Access = cls.env["ir.access"]
        # A model that certainly exists and that no test will mutate.
        cls.model = cls.env["ir.model"]._get("res.partner.category")
        # _get() returns an empty recordset for an unknown model, which would
        # surface much later as a NOT NULL violation on ir_access.model_id
        assert cls.model, "fixture model res.partner.category is missing"

        cls.leaf = cls.Groups.create({"name": "MDX Test Leaf"})
        cls.middle = cls.Groups.create({"name": "MDX Test Middle",
                                        "implied_ids": [(6, 0, cls.leaf.ids)]})
        cls.top = cls.Groups.create({"name": "MDX Test Top",
                                     "implied_ids": [(6, 0, cls.middle.ids)]})

    def _clear(self):
        self.Access.with_context(active_test=False).search(
            [("model_id", "=", self.model.id)]).unlink()

    def _grant(self, group, operation, domain=False, **extra):
        return self.Access.create(dict({
            "name": "mdx test %s %s" % (group.name if group else "restriction", operation),
            "model_id": self.model.id,
            "group_id": group.id if group else False,
            "operation": operation,
            "domain": domain,
        }, **extra))

    def _perms(self, groups):
        ids = groups.ids if hasattr(groups, "ids") else groups
        return self.Engine._perms_by_model(ids, model_names=[self.model.model])

    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    def test_model_without_permission_is_denied(self):
        """Odoo denies a model that no permission reaches."""
        self._clear()
        self.assertNotIn(self.model.model, self._perms(self.top),
                         "a model with no access rule must not appear as granted")

    def test_permission_grants_only_its_operations(self):
        self._clear()
        self._grant(self.leaf, "r")
        perms = self._perms(self.Engine._expand_groups(self.top))
        self.assertTrue(perms[self.model.model]["read"])
        self.assertFalse(perms[self.model.model]["write"])
        self.assertFalse(perms[self.model.model]["create"])
        self.assertFalse(perms[self.model.model]["unlink"])

    def test_operation_letters_map_to_modes(self):
        self._clear()
        self._grant(self.leaf, "cud")
        perms = self._perms(self.leaf)[self.model.model]
        self.assertEqual(
            perms, {"read": False, "write": True, "create": True, "unlink": True},
            "'u' is write and 'd' is delete; 'r' was not granted")

    def test_access_is_inherited_through_implication(self):
        """The grant sits on the leaf; the user holds only the top group."""
        self._clear()
        self._grant(self.leaf, "r")
        self.assertNotIn(self.model.model, self._perms(self.top),
                         "resolving the unexpanded set must not see the leaf's grant")
        expanded = self._perms(self.Engine._expand_groups(self.top))
        self.assertTrue(expanded[self.model.model]["read"],
                        "expanding the group set must pick up the implied group's grant")

    def test_a_restriction_grants_nothing(self):
        """Since Odoo 20 a rule without a group narrows; it no longer grants."""
        self._clear()
        self._grant(False, "r")
        self.assertNotIn(self.model.model, self._perms([]),
                         "a group-less rule must not grant access to a user with no groups")
        self.assertNotIn(self.model.model, self._perms(self.leaf),
                         "nor to a user who holds groups but no permission")

    def test_a_restriction_does_not_remove_model_access(self):
        self._clear()
        self._grant(self.leaf, "r")
        self._grant(False, "r", domain="[('id', '>', 0)]")
        self.assertTrue(self._perms(self.leaf)[self.model.model]["read"],
                        "a restriction narrows the records, it does not deny the model")

    def test_an_unsatisfiable_restriction_denies(self):
        self._clear()
        self._grant(self.leaf, "ru")
        self._grant(False, "u", domain="[(0, '=', 1)]")
        perms = self._perms(self.leaf)[self.model.model]
        self.assertTrue(perms["read"], "the restriction only covers update")
        self.assertFalse(perms["write"], "a restriction nothing can satisfy denies the operation")

    def test_an_unsatisfiable_permission_grants_nothing(self):
        self._clear()
        self._grant(self.leaf, "r", domain="[(0, '=', 1)]")
        self.assertNotIn(self.model.model, self._perms(self.leaf))

    def test_inactive_access_grants_nothing(self):
        self._clear()
        self._grant(self.leaf, "r", active=False)
        self.assertNotIn(self.model.model, self._perms(self.leaf))

    def test_access_operator_follows_the_related_model(self):
        """('parent_id', 'access', 'write') is false without write on the comodel."""
        self._clear()
        self._grant(self.leaf, "r", domain="[('parent_id', 'access', 'write')]")
        self.assertNotIn(
            self.model.model, self._perms(self.leaf),
            "read is delegated to write on the same model, which nobody was granted")
        self._grant(self.leaf, "u")
        perms = self._perms(self.leaf)[self.model.model]
        self.assertTrue(perms["read"], "once write is granted the delegated read follows")

    # ------------------------------------------------------------------
    def _assert_matches_server(self, user):
        _explicit, effective = self.Engine._user_group_sets(user)
        mine = self.Engine._perms_by_model(effective.ids)
        coded = self.Engine._models_with_coded_access()
        mismatches = []
        for model_name, model_class in self.env.registry.items():
            if model_class._abstract or model_name in coded:
                continue
            model = self.env[model_name].with_user(user)
            for mode in MODES:
                theirs = model.has_access(mode)
                ours = bool(mine.get(model_name, {}).get(mode))
                if theirs != ours:
                    mismatches.append((model_name, mode, "server=%s engine=%s" % (theirs, ours)))
        self.assertFalse(
            mismatches,
            "the engine must agree with Model.has_access for %s on every model and "
            "operation" % user.login)

    def test_engine_matches_the_server_for_a_custom_group(self):
        self._clear()
        self._grant(self.leaf, "ru")
        self._grant(False, "r", domain="[('id', '>', 0)]")
        user = self.env["res.users"].create({
            "name": "MDX Engine Probe", "login": "mdx_engine_probe",
            "group_ids": [(6, 0, self.top.ids)],
        })
        self._assert_matches_server(user)

    def test_engine_matches_the_server_for_an_internal_user(self):
        user = self.env["res.users"].create({
            "name": "MDX Internal Probe", "login": "mdx_internal_probe",
            "group_ids": [(6, 0, self.env.ref("base.group_user").ids)],
        })
        self._assert_matches_server(user)

    def test_engine_matches_the_server_for_a_portal_user(self):
        user = self.env["res.users"].create({
            "name": "MDX Portal Probe", "login": "mdx_portal_probe",
            "group_ids": [(6, 0, self.env.ref("base.group_portal").ids)],
        })
        self._assert_matches_server(user)

    def test_engine_matches_the_server_for_an_administrator(self):
        self._assert_matches_server(self.env.ref("base.user_admin"))

    # ------------------------------------------------------------------
    def test_accesses_split_restrictions_and_permissions(self):
        self._clear()
        restriction = self._grant(False, "r", domain="[('id', '>', 0)]")
        permission = self._grant(self.leaf, "r")
        restrictions, permissions = self.Engine._accesses_for(
            self.model.model, self.leaf.ids, "read")
        self.assertIn(restriction, restrictions)
        self.assertIn(permission, permissions)
        self.assertNotIn(permission, restrictions)

        # a permission whose group the user does not hold is not applied at all
        restrictions2, permissions2 = self.Engine._accesses_for(self.model.model, [], "read")
        self.assertNotIn(permission, permissions2)
        self.assertIn(restriction, restrictions2, "a restriction applies to everyone")

        # and one that does not cover the operation is not selected for it
        _r3, permissions3 = self.Engine._accesses_for(self.model.model, self.leaf.ids, "write")
        self.assertNotIn(permission, permissions3)

    def test_explain_reports_permissions_and_restrictions(self):
        self._clear()
        self._grant(self.leaf, "r", domain="[('active', '=', True)]")
        self._grant(False, "r", domain="[('id', '>', 0)]")
        self.env.user.group_ids = [
            (4, self.env.ref("mdx_access_manager.group_access_manager").id)]
        user = self.env["res.users"].create({
            "name": "MDX Explain Probe", "login": "mdx_explain_probe",
            "group_ids": [(6, 0, self.top.ids)],
        })
        matrix = self.env["mdx.access.matrix"].create({
            "user_id": user.id, "model_filter": self.model.model})
        matrix.action_run()
        line = matrix.line_ids.filtered(lambda l: l.model_name == self.model.model)
        self.assertTrue(line.can_read)
        self.assertFalse(line.can_write)
        line.action_explain()
        sources = line.provenance_ids.mapped("source")
        self.assertEqual(sorted(sources), ["permission", "restriction"])
        permission = line.provenance_ids.filtered(lambda p: p.source == "permission")
        self.assertIn("MDX Test Top", permission.via, "the path must start at the assigned group")
        self.assertIn("MDX Test Leaf", permission.via)
        self.assertTrue(line.has_restriction)

    def test_group_label_is_privilege_qualified(self):
        privilege = self.env["res.groups.privilege"].create({"name": "MDX Test Privilege"})
        group = self.Groups.create({"name": "Manager", "privilege_id": privilege.id})
        self.assertEqual(self.Engine._group_label(group), "MDX Test Privilege / Manager")

    def test_implication_path_explains_inheritance(self):
        paths = self.Engine._implication_paths(self.top, self.leaf)
        self.assertTrue(paths)
        self.assertIn("MDX Test Top", paths[0])
        self.assertIn("MDX Test Leaf", paths[0])
