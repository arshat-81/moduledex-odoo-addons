"""The dry run must be honest: it may not write, and it must predict correctly."""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestChangeSet(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Groups = cls.env["res.groups"]
        cls.Access = cls.env["ir.model.access"]
        cls.ChangeSet = cls.env["mdx.access.change.set"]
        cls.model = cls.env["ir.model"]._get("res.partner.category")
        # _get() returns an empty recordset for an unknown model, which would
        # surface much later as a NOT NULL violation on ir_model_access.model_id
        assert cls.model, "fixture model res.partner.category is missing"

        cls.granting = cls.Groups.create({"name": "MDX CS Granting"})
        cls.wrapper = cls.Groups.create({"name": "MDX CS Wrapper",
                                         "implied_ids": [(6, 0, cls.granting.ids)]})
        cls.Access.search([("model_id", "=", cls.model.id)]).unlink()
        cls.Access.create({
            "name": "mdx cs grant",
            "model_id": cls.model.id,
            "group_id": cls.granting.id,
            "perm_read": True,
            "perm_write": True,
        })
        cls.user = cls.env["res.users"].create({
            "name": "MDX CS Subject",
            "login": "mdx_cs_subject",
        })
        cls.env.user.groups_id = [(4, cls.env.ref("mdx_access_manager.group_access_manager").id)]

    def _change(self, **vals):
        return self.ChangeSet.create(dict({"user_id": self.user.id}, **vals))

    def test_preview_does_not_write(self):
        """The single safety property this feature rests on."""
        before = set(self.user.groups_id.ids)
        change = self._change(add_group_ids=[(6, 0, self.wrapper.ids)])
        change.action_preview()
        self.user.invalidate_recordset()
        self.assertEqual(set(self.user.groups_id.ids), before,
                         "previewing must not touch the user's groups")
        self.assertEqual(change.state, "previewed")

    def test_preview_predicts_the_gain(self):
        change = self._change(add_group_ids=[(6, 0, self.wrapper.ids)])
        change.action_preview()
        line = change.line_ids.filtered(lambda l: l.model_name == self.model.model)
        self.assertTrue(line, "the model granted through the implied group must be predicted")
        self.assertEqual(line.change, "gained")
        self.assertIn("Read", line.gained_modes)
        self.assertIn("Write", line.gained_modes)
        self.assertTrue(change.gained_count)

    def test_warning_names_the_implied_groups(self):
        change = self._change(add_group_ids=[(6, 0, self.wrapper.ids)])
        change.action_preview()
        self.assertIn("MDX CS Granting", change.warning or "",
                      "adding a wrapper group must disclose what it pulls in")

    def test_cannot_add_and_remove_the_same_group(self):
        with self.assertRaises(UserError):
            self._change(add_group_ids=[(6, 0, self.wrapper.ids)],
                         remove_group_ids=[(6, 0, self.wrapper.ids)])

    def test_empty_change_is_rejected(self):
        change = self._change()
        with self.assertRaises(UserError):
            change.action_preview()

    def test_apply_requires_a_preview(self):
        change = self._change(add_group_ids=[(6, 0, self.wrapper.ids)])
        with self.assertRaises(UserError):
            change.action_apply()

    def test_apply_writes_and_logs(self):
        change = self._change(add_group_ids=[(6, 0, self.wrapper.ids)])
        change.action_preview()
        change.action_apply()
        self.user.invalidate_recordset()
        self.assertIn(self.wrapper, self.user.groups_id)
        self.assertEqual(change.state, "applied")
        self.assertTrue(change.applied_by_id)
        log = self.env["mdx.access.change.log"].search([
            ("change_set_id", "=", change.id)], limit=1)
        self.assertTrue(log, "applying must leave an audit row")
        self.assertIn("MDX CS Wrapper", log.after_json or "")

    def test_prediction_matches_reality(self):
        """What the preview promised is what applying actually produces."""
        Engine = self.env["mdx.access.engine"]
        change = self._change(add_group_ids=[(6, 0, self.wrapper.ids)])
        change.action_preview()
        predicted = {
            line.model_name: line.gained_modes for line in change.line_ids
            if line.change == "gained"
        }
        before = Engine._perms_by_model(Engine._user_group_sets(self.user)[1].ids)
        change.action_apply()
        self.user.invalidate_recordset()
        after = Engine._perms_by_model(Engine._user_group_sets(self.user)[1].ids)
        actually_gained = {
            name for name, perms in after.items()
            if any(perms[m] and not before.get(name, {}).get(m) for m in ("read", "write",
                                                                          "create", "unlink"))
        }
        self.assertEqual(set(predicted), actually_gained,
                         "the dry run must predict exactly what applying does")

    def test_applied_change_cannot_be_reopened(self):
        change = self._change(add_group_ids=[(6, 0, self.wrapper.ids)])
        change.action_preview()
        change.action_apply()
        with self.assertRaises(UserError):
            change.action_reset()
