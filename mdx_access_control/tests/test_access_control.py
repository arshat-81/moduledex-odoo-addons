"""Every rule is checked against what the server does, for a restricted user and a control."""

import contextlib
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAccessControl(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Profile = cls.env["mdx.access.profile"]
        cls.Access = cls.env["ir.access"]
        # Contact Creation to write partners, Export so that Odoo's own export
        # gate is open and the tests exercise this module's.
        groups = (cls.env.ref("base.group_user") | cls.env.ref("base.group_partner_manager")
                  | cls.env.ref("base.group_allow_export"))
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.user = Users.create({
            "name": "MDX Restricted", "login": "mdx_restricted", "group_ids": [(6, 0, groups.ids)]})
        cls.other = Users.create({
            "name": "MDX Control", "login": "mdx_control", "group_ids": [(6, 0, groups.ids)]})
        cls.admin = cls.env.ref("base.user_admin")
        cls.partner_model = cls.env["ir.model"]._get("res.partner")
        cls.category_model = cls.env["ir.model"]._get("res.partner.category")
        Category = cls.env["res.partner.category"]
        cls.keep = Category.create({"name": "MDX Keep"})
        cls.drop = Category.create({"name": "MDX Drop"})

    def setUp(self):
        super().setUp()
        # The blocked-attempt log writes on its own cursor so that it survives
        # the refusal. A second cursor cannot see this test's uncommitted
        # users, so here it writes in the test transaction instead.
        env = self.env

        @contextlib.contextmanager
        def _log_env(_self):
            yield env(su=True)

        patcher = patch.object(type(self.env["mdx.access.denial"]), "_log_env", _log_env)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ---------------------------------------------------------
    def _profile(self, enforce=True, **vals):
        profile = self.Profile.create(dict({
            "name": "MDX Test Profile", "user_ids": [(6, 0, self.user.ids)]}, **vals))
        if enforce:
            profile.action_enforce()
        return profile

    def _field(self, name, model=None):
        model = model or self.partner_model
        field = self.env["ir.model.fields"].search(
            [("model_id", "=", model.id), ("name", "=", name)], limit=1)
        self.assertTrue(field, "fixture field %s is missing" % name)
        return field

    def _arch(self, user, view_type="form", model="res.partner"):
        result = self.env[model].with_user(user).get_view(view_type=view_type)
        return etree.fromstring(result["arch"])

    def _as(self, user, model="res.partner.category"):
        return self.env[model].with_user(user)

    # -- membership ------------------------------------------------------
    def test_membership_by_user_and_group(self):
        group = self.env["res.groups"].create({"name": "MDX Membership Group"})
        self.other.group_ids = [(4, group.id)]
        profile = self._profile(enforce=False, user_ids=[(6, 0, self.user.ids)],
                                group_ids=[(6, 0, group.ids)])
        self.assertEqual(profile._members(), self.user | self.other)
        self.assertEqual(profile.member_count, 2)

    def test_administrators_are_left_out_by_default(self):
        profile = self._profile(
            user_ids=[(6, 0, (self.user | self.admin).ids)],
            model_rule_ids=[(0, 0, {"model_id": self.category_model.id, "no_create": True})])
        self.assertNotIn(self.admin, profile._members())
        self.assertTrue(self._as(self.admin).has_access("create"),
                        "an administrator must not be locked out by a profile")
        self.assertFalse(self._as(self.user).has_access("create"))

        profile.include_admins = True
        self.assertIn(self.admin, profile._members())
        self.assertFalse(self._as(self.admin).has_access("create"),
                         "opting administrators in must bind them")

    def test_group_membership_follows_implication(self):
        leaf = self.env["res.groups"].create({"name": "MDX Leaf"})
        top = self.env["res.groups"].create({"name": "MDX Top", "implied_ids": [(6, 0, leaf.ids)]})
        self.other.group_ids = [(4, top.id)]
        self._profile(user_ids=[(5, 0, 0)], group_ids=[(6, 0, leaf.ids)],
                      model_rule_ids=[(0, 0, {"model_id": self.category_model.id,
                                              "no_unlink": True})])
        self.assertFalse(self._as(self.other).has_access("unlink"),
                         "holding a group that implies the profile's group is membership")
        self.assertTrue(self._as(self.user).has_access("unlink"))

    # -- models and records: native restrictions --------------------------
    def test_block_operations_creates_native_restrictions(self):
        profile = self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_create": True, "no_unlink": True})])
        rows = self.Access.search([("mdx_profile_id", "=", profile.id)])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows.group_id, "a restriction carries no group")
        self.assertEqual(rows.operation, "cd")
        self.assertEqual(rows.kind, "restriction")

        restricted = self._as(self.user)
        self.assertFalse(restricted.has_access("create"))
        self.assertFalse(restricted.has_access("unlink"))
        self.assertTrue(restricted.has_access("write"), "edit was not blocked")
        self.assertTrue(restricted.has_access("read"))
        with self.assertRaises(AccessError):
            restricted.create({"name": "MDX Must Fail"})

        control = self._as(self.other)
        self.assertTrue(control.has_access("create"))
        self.assertTrue(control.has_access("unlink"))

    def test_draft_profile_restricts_nobody(self):
        profile = self._profile(enforce=False, model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_create": True})])
        self.assertFalse(self.Access.search([("mdx_profile_id", "=", profile.id)]))
        self.assertTrue(self._as(self.user).has_access("create"))

    def test_suspend_restores_access(self):
        profile = self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_create": True})])
        self.assertFalse(self._as(self.user).has_access("create"))
        profile.action_suspend()
        self.assertFalse(self.Access.search([("mdx_profile_id", "=", profile.id)]))
        self.assertTrue(self._as(self.user).has_access("create"))

    def test_deleting_a_profile_removes_its_restrictions(self):
        profile = self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_create": True})])
        profile_id = profile.id
        profile.unlink()
        self.assertFalse(self.Access.with_context(active_test=False).search(
            [("mdx_profile_id", "=", profile_id)]))
        self.assertTrue(self._as(self.user).has_access("create"))

    def test_read_filter_limits_records(self):
        self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "read_domain": "[('name', '=', 'MDX Keep')]"})])
        mine = [("id", "in", (self.keep | self.drop).ids)]
        self.assertEqual(self._as(self.user).search(mine), self.keep)
        self.assertEqual(self._as(self.other).search(mine), self.keep | self.drop)
        with self.assertRaises(AccessError):
            self.drop.with_user(self.user).read(["name"])

    def test_edit_filter_limits_writes_but_not_reads(self):
        self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "write_domain": "[('name', '=', 'MDX Keep')]"})])
        self.assertTrue(self.keep.with_user(self.user).has_access("write"))
        self.assertFalse(self.drop.with_user(self.user).has_access("write"))
        self.assertTrue(self.drop.with_user(self.user).has_access("read"))
        with self.assertRaises(AccessError):
            self.drop.with_user(self.user).write({"name": "MDX Changed"})
        self.assertTrue(self.drop.with_user(self.other).has_access("write"))

    def test_filter_can_use_the_user(self):
        mine = self.env["res.partner.category"].with_user(self.user).create({"name": "MDX Mine"})
        self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id,
            "read_domain": "[('create_uid', '=', user.id)]"})])
        visible = self._as(self.user).search([("id", "in", (mine | self.keep).ids)])
        self.assertEqual(visible, mine)

    def test_rule_edits_take_effect_immediately(self):
        profile = self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_create": True})])
        self.assertTrue(self._as(self.user).has_access("unlink"))
        profile.model_rule_ids.write({"no_create": False, "no_unlink": True})
        self.assertTrue(self._as(self.user).has_access("create"))
        self.assertFalse(self._as(self.user).has_access("unlink"))
        profile.model_rule_ids.unlink()
        self.assertTrue(self._as(self.user).has_access("unlink"))

    def test_membership_edits_take_effect_immediately(self):
        profile = self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_create": True})])
        self.assertTrue(self._as(self.other).has_access("create"))
        profile.user_ids = [(4, self.other.id)]
        self.assertFalse(self._as(self.other).has_access("create"))
        profile.user_ids = [(3, self.user.id)]
        self.assertTrue(self._as(self.user).has_access("create"))

    def test_invalid_domain_is_refused_on_save(self):
        with self.assertRaises(ValidationError):
            self._profile(enforce=False, model_rule_ids=[(0, 0, {
                "model_id": self.category_model.id,
                "read_domain": "[('no_such_field', '=', 1)]"})])
        with self.assertRaises(ValidationError):
            self._profile(enforce=False, model_rule_ids=[(0, 0, {
                "model_id": self.category_model.id, "read_domain": "not a domain"})])

    def test_access_configuration_cannot_be_restricted(self):
        with self.assertRaises(ValidationError):
            self._profile(enforce=False, model_rule_ids=[(0, 0, {
                "model_id": self.env["ir.model"]._get("ir.access").id, "no_create": True})])

    def test_enforce_needs_members_and_rules(self):
        with self.assertRaises(UserError):
            self.Profile.create({"name": "MDX Empty"}).action_enforce()
        with self.assertRaises(UserError):
            self._profile(enforce=False).action_enforce()

    # -- fields ------------------------------------------------------------
    def test_field_flags_reach_the_view(self):
        self._profile(field_rule_ids=[
            (0, 0, {"model_id": self.partner_model.id, "field_id": self._field("phone").id,
                    "invisible": True}),
            (0, 0, {"model_id": self.partner_model.id, "field_id": self._field("email").id,
                    "readonly": True}),
            (0, 0, {"model_id": self.partner_model.id, "field_id": self._field("website").id,
                    "required": True}),
        ])
        arch = self._arch(self.user)
        phones = arch.xpath("//field[@name='phone']")
        self.assertTrue(phones, "fixture: the partner form shows phone")
        self.assertTrue(all(node.get("invisible") == "True" for node in phones),
                        "every occurrence must be hidden, embedded views included")
        self.assertTrue(all(node.get("readonly") == "True"
                            for node in arch.xpath("//field[@name='email']")))
        self.assertTrue(all(node.get("required") == "True"
                            for node in arch.xpath("//field[@name='website']")))

        control = self._arch(self.other)
        self.assertTrue(all(node.get("invisible") != "True"
                            for node in control.xpath("//field[@name='phone']")),
                        "a user outside the profile must get the untouched view")

    def test_hidden_field_is_a_hidden_column_in_lists(self):
        self._profile(field_rule_ids=[(0, 0, {
            "model_id": self.partner_model.id, "field_id": self._field("email").id,
            "invisible": True})])
        columns = self._arch(self.user, "list").xpath("//field[@name='email']")
        self.assertTrue(columns, "fixture: the partner list shows email")
        self.assertTrue(all(node.get("column_invisible") == "True" for node in columns))

    def test_hidden_field_leaves_search_and_field_metadata(self):
        self._profile(field_rule_ids=[(0, 0, {
            "model_id": self.partner_model.id, "field_id": self._field("user_id").id,
            "invisible": True})])
        control = self._arch(self.other, "search")
        self.assertTrue(
            control.xpath("//field[@name='user_id']") or
            control.xpath("//filter[contains(@context, 'user_id')]"),
            "fixture: the partner search view uses user_id")
        restricted = self._arch(self.user, "search")
        self.assertFalse(restricted.xpath("//field[@name='user_id']"))
        self.assertFalse(restricted.xpath("//filter[contains(@context, \"'user_id'\")]"))

        description = self.env["res.partner"].with_user(self.user).fields_get(
            ["user_id", "name"])
        self.assertFalse(description["user_id"]["searchable"])
        self.assertFalse(description["user_id"]["sortable"])
        self.assertTrue(description["name"]["searchable"], "other fields are untouched")
        control_description = self.env["res.partner"].with_user(self.other).fields_get(["user_id"])
        self.assertTrue(control_description["user_id"]["searchable"])

    def test_strict_readonly_rejects_writes_on_the_server(self):
        partner = self.env["res.partner"].create({"name": "MDX Strict", "phone": "1"})
        profile = self._profile(field_rule_ids=[(0, 0, {
            "model_id": self.partner_model.id, "field_id": self._field("phone").id,
            "readonly": True})])
        partner.with_user(self.user).write({"phone": "2"})
        self.assertEqual(partner.phone, "2", "without the strict option the server accepts it")

        profile.field_rule_ids.strict = True
        with self.assertRaises(AccessError):
            partner.with_user(self.user).write({"phone": "3"})
        partner.with_user(self.user).write({"name": "MDX Strict Renamed"})
        partner.with_user(self.other).write({"phone": "4"})
        self.assertEqual(partner.phone, "4")

    def test_two_profiles_add_up(self):
        self._profile(name="MDX A", field_rule_ids=[(0, 0, {
            "model_id": self.partner_model.id, "field_id": self._field("phone").id,
            "readonly": True})])
        self._profile(name="MDX B", field_rule_ids=[(0, 0, {
            "model_id": self.partner_model.id, "field_id": self._field("phone").id,
            "invisible": True})])
        node = self._arch(self.user).xpath("//field[@name='phone']")[0]
        self.assertEqual((node.get("invisible"), node.get("readonly")), ("True", "True"))

    def test_field_rule_needs_an_effect(self):
        with self.assertRaises(ValidationError):
            self._profile(enforce=False, field_rule_ids=[(0, 0, {
                "model_id": self.partner_model.id, "field_id": self._field("phone").id})])
        with self.assertRaises(ValidationError):
            self._profile(enforce=False, field_rule_ids=[(0, 0, {
                "model_id": self.partner_model.id, "field_id": self._field("phone").id,
                "required": True, "invisible": True})])

    # -- buttons, tabs, chatter, bindings ----------------------------------
    def _scan(self, profile):
        picker = self.env["mdx.access.element.picker"].create({
            "profile_id": profile.id, "model_id": self.partner_model.id})
        picker.action_scan()
        return picker

    def test_picker_lists_and_hides_view_elements(self):
        profile = self._profile(enforce=False)
        picker = self._scan(profile)
        pages = picker.line_ids.filtered(lambda line: line.kind == "page")
        buttons = picker.line_ids.filtered(lambda line: line.kind == "button")
        self.assertTrue(pages, "the partner form has named tabs")
        self.assertTrue(buttons, "the partner views have named buttons")

        # The picker lists every view of the model, including ones this user is
        # never served. Pick elements the control user really gets.
        control = self._arch(self.other)
        page = next((line for line in pages
                     if control.xpath("//page[@name='%s']" % line.name)), None)
        button = next((line for line in buttons
                       if control.xpath("//button[@name='%s']" % line.name)), None)
        self.assertTrue(page, "fixture: the partner form shows a named tab")
        self.assertTrue(button, "fixture: the partner form shows a named button")
        (page | button).write({"selected": True})
        picker.action_add()
        self.assertEqual(len(profile.element_rule_ids), 2)
        profile.action_enforce()

        restricted = self._arch(self.user)
        hidden_page = restricted.xpath("//page[@name='%s']" % page.name)
        # A hidden tab stays in the view, invisible: conditions elsewhere on
        # the form can depend on the fields it holds.
        self.assertTrue(hidden_page, "a hidden tab must stay in the arch")
        self.assertTrue(all(node.get("invisible") == "True" for node in hidden_page))
        self.assertFalse(restricted.xpath("//button[@name='%s']" % button.name))
        control_page = self._arch(self.other).xpath("//page[@name='%s']" % page.name)
        self.assertTrue(all(node.get("invisible") != "True" for node in control_page))

        again = self._scan(profile)
        self.assertTrue(again.line_ids.filtered(
            lambda line: line.kind == "page" and line.name == page.name).already_hidden)

    def test_chatter_can_be_hidden(self):
        self.assertTrue(self._arch(self.other).xpath("//chatter"),
                        "fixture: the partner form has a chatter")
        self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.partner_model.id, "hide_chatter": True})])
        self.assertFalse(self._arch(self.user).xpath("//chatter"))
        self.assertTrue(self._arch(self.other).xpath("//chatter"))

    def test_bound_action_can_be_hidden(self):
        action = self.env["ir.actions.server"].create({
            "name": "MDX Bound Action", "model_id": self.partner_model.id,
            "binding_model_id": self.partner_model.id, "state": "code", "code": "pass",
        })
        Actions = self.env["ir.actions.actions"]

        def bound(user):
            return [a["id"] for a in Actions.with_user(user).get_bindings("res.partner")
                    .get("action", [])]

        self.assertIn(action.id, bound(self.user))
        self._profile(element_rule_ids=[(0, 0, {
            "model_id": self.partner_model.id, "kind": "action", "name": str(action.id),
            "label": action.name})])
        self.assertNotIn(action.id, bound(self.user))
        self.assertIn(action.id, bound(self.other))

    # -- menus ---------------------------------------------------------------
    def test_hidden_menu_takes_its_children(self):
        action = self.env["ir.actions.act_window"].create({
            "name": "MDX Tags", "res_model": "res.partner.category", "view_mode": "list,form"})
        Menu = self.env["ir.ui.menu"]
        root = Menu.create({"name": "MDX Root"})
        child = Menu.create({
            "name": "MDX Child", "parent_id": root.id,
            "action": "ir.actions.act_window,%s" % action.id})
        other_root = Menu.create({"name": "MDX Other Root"})
        other_child = Menu.create({
            "name": "MDX Other Child", "parent_id": other_root.id,
            "action": "ir.actions.act_window,%s" % action.id})

        def visible(user):
            return Menu.with_user(user)._visible_menu_ids()

        self.assertIn(child.id, visible(self.user))
        self._profile(menu_ids=[(6, 0, root.ids)])
        self.assertNotIn(root.id, visible(self.user))
        self.assertNotIn(child.id, visible(self.user), "children go with their parent")
        self.assertIn(other_child.id, visible(self.user))
        self.assertIn(child.id, visible(self.other))
        loaded = Menu.with_user(self.user).load_menus(False)
        self.assertNotIn(child.id, loaded)
        self.assertIn(other_child.id, loaded)

    # -- preview -------------------------------------------------------------
    def test_preview_counts_what_the_user_would_keep(self):
        profile = self._profile(enforce=False, model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_unlink": True,
            "read_domain": "[('name', '=', 'MDX Keep')]"})],
            field_rule_ids=[(0, 0, {
                "model_id": self.partner_model.id, "field_id": self._field("phone").id,
                "invisible": True})])
        total = self._as(self.user).search_count([])
        preview = self.env["mdx.access.preview"].create({
            "profile_id": profile.id, "user_id": self.user.id})
        self.assertTrue(preview.applies)
        html = str(preview.report_html)
        self.assertIn("1 of the %s record(s)" % total, html)
        self.assertIn("draft", html)
        self.assertIn("res.partner.phone", html)
        self.assertTrue(self._as(self.user).has_access("unlink"), "a preview must not enforce")

        for_admin = self.env["mdx.access.preview"].create({
            "profile_id": profile.id, "user_id": self.admin.id})
        self.assertFalse(for_admin.applies)

    # -- role tiers ----------------------------------------------------------
    def test_role_scope_targets_a_whole_tier(self):
        light = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "MDX Light", "login": "mdx_light",
            "group_ids": [(6, 0, self.env.ref("base.group_user").ids)]})
        self.assertEqual(light.role, "light_user", "fixture: a user with only light groups")
        self.assertEqual(self.user.role, "regular_user", "fixture: Contact Creation is regular")
        profile = self._profile(user_ids=[(5, 0, 0)], role_scope="light_user",
                                model_rule_ids=[(0, 0, {
                                    "model_id": self.category_model.id,
                                    "read_domain": "[('name', '=', 'MDX Keep')]"})])
        self.assertIn(light, profile._members())
        self.assertNotIn(self.user, profile._members())
        mine = [("id", "in", (self.keep | self.drop).ids)]
        self.assertEqual(self._as(light).search(mine), self.keep,
                         "the native restriction must bind the whole tier")
        self.assertEqual(self._as(self.user).search(mine), self.keep | self.drop)

    def test_role_derived_from_groups_matches_core(self):
        """The hooks derive the tier from group ids; core computes it. They must agree."""
        snapshot = self.Profile._rules_snapshot()
        for user in self.env["res.users"].search([("share", "=", False)]):
            derived = self.Profile._role_of(frozenset(user._get_group_ids()), snapshot)
            self.assertEqual(derived, user.role, "role of %s" % user.login)

    # -- policies --------------------------------------------------------------
    def test_read_only_blocks_writes_everywhere_but_not_reads(self):
        partner = self.env["res.partner"].create({"name": "MDX Read Only Target"})
        self._profile(read_only=True)
        for model in ("res.partner", "res.partner.category", "res.country"):
            restricted = self.env[model].with_user(self.user)
            self.assertTrue(restricted.has_access("read"), "%s must stay readable" % model)
            for operation in ("write", "create", "unlink"):
                self.assertFalse(restricted.has_access(operation), "%s %s" % (model, operation))
        with self.assertRaises(AccessError) as caught:
            partner.with_user(self.user).write({"name": "MDX Changed"})
        self.assertIn("MDX Test Profile", str(caught.exception),
                      "the refusal must name the profile responsible")
        self.assertEqual(partner.with_user(self.user).name, "MDX Read Only Target")
        self.assertTrue(self.env["res.partner"].with_user(self.other).has_access("write"))

    def test_read_only_leaves_personal_settings_and_wizards_writable(self):
        self._profile(read_only=True)
        self.assertTrue(self.env["res.users.settings"].with_user(self.user).has_access("write"))
        self.assertTrue(self.env["ir.filters"].with_user(self.user).has_access("create"))
        wizard = self.env["base.language.export"]
        if wizard.with_user(self.other).has_access("create"):
            self.assertTrue(wizard.with_user(self.user).has_access("create"),
                            "wizards hold no business data and must keep working")

    def test_export_and_import_are_refused_by_the_server(self):
        self._profile(block_export=True, block_import=True)
        partners = self.env["res.partner"].search([], limit=2)
        with self.assertRaises(AccessError):
            partners.with_user(self.user).export_data(["name"])
        with self.assertRaises(AccessError):
            self.env["res.partner.category"].with_user(self.user).load(["name"], [["MDX Imported"]])
        exported = partners.with_user(self.other).export_data(["name"])
        self.assertEqual(len(exported["datas"]), len(partners))
        loaded = self.env["res.partner.category"].with_user(self.other).load(
            ["name"], [["MDX Imported"]])
        self.assertTrue(loaded["ids"])

    def test_api_login_is_refused_but_interactive_login_is_not(self):
        from odoo.exceptions import AccessDenied  # noqa: PLC0415
        self.user.password = "mdx-Password-1"
        self.other.password = "mdx-Password-2"
        credential = {"type": "password", "login": self.user.login, "password": "mdx-Password-1"}
        self.user.with_user(self.user)._check_credentials(credential, {"interactive": False})

        self._profile(block_api=True)
        with self.assertRaises(AccessDenied):
            self.user.with_user(self.user)._check_credentials(credential, {"interactive": False})
        info = self.user.with_user(self.user)._check_credentials(credential, {"interactive": True})
        self.assertEqual(info["uid"], self.user.id, "the browser login must keep working")
        self.other.with_user(self.other)._check_credentials(
            {"type": "password", "login": self.other.login, "password": "mdx-Password-2"},
            {"interactive": False})
        with self.assertRaises(AccessDenied):
            self.user.with_user(self.user)._check_credentials(
                dict(credential, password="wrong"), {"interactive": True})

    def test_api_key_is_refused_for_a_blocked_user(self):
        Keys = self.env["res.users.apikeys"]
        key = Keys.with_user(self.user).sudo()._generate("rpc", "MDX key", False)
        self.assertEqual(Keys._check_credentials(scope="rpc", key=key), self.user.id)
        self._profile(block_api=True)
        self.assertFalse(Keys._check_credentials(scope="rpc", key=key),
                         "an API key must stop working for a blocked user")

    # -- blocked attempts ------------------------------------------------------
    def test_blocked_attempts_are_logged_with_the_profile(self):
        Denial = self.env["mdx.access.denial"]
        profile = self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_create": True,
            "write_domain": "[('name', '=', 'MDX Keep')]"})])
        # Not assertRaises: Odoo's wraps the block in a savepoint and rolls it
        # back, which would discard the very rows this test is about. (In
        # production they are written on a separate cursor for that reason.)
        def refused(action):
            try:
                action()
            except AccessError as error:
                return str(error)
            self.fail("the operation should have been refused")

        message = refused(lambda: self._as(self.user).create({"name": "MDX Must Fail"}))
        self.assertIn(profile.name, message)
        refused(lambda: self.drop.with_user(self.user).write({"name": "MDX Changed"}))
        refused(lambda: self.drop.with_user(self.user).write({"name": "MDX Changed Again"}))

        created = Denial.search([("user_id", "=", self.user.id), ("operation", "=", "create")])
        self.assertEqual(len(created), 1)
        self.assertEqual(created.profile_id, profile)
        self.assertEqual(created.model, "res.partner.category")
        written = Denial.search([("user_id", "=", self.user.id), ("operation", "=", "write")])
        self.assertEqual(len(written), 1, "repeats inside the window are merged")
        self.assertEqual(written.count, 2)
        self.assertIn(str(self.drop.id), written.res_ids)

    def test_refusals_unrelated_to_a_profile_are_not_attributed(self):
        self._profile(model_rule_ids=[(0, 0, {
            "model_id": self.category_model.id, "no_create": True})])
        before = self.env["mdx.access.denial"].search_count([])
        with self.assertRaises(AccessError) as caught:
            self.env["ir.config_parameter"].with_user(self.user).create(
                {"key": "mdx.probe", "value": "x"})
        self.assertNotIn("access profile", str(caught.exception))
        self.assertEqual(self.env["mdx.access.denial"].search_count([]), before)

    # -- validity window -------------------------------------------------------
    def test_schedule_starts_and_ends_a_profile(self):
        from datetime import timedelta  # noqa: PLC0415
        from odoo import fields as odoo_fields  # noqa: PLC0415
        now = odoo_fields.Datetime.now()
        profile = self._profile(enforce=False, valid_from=now + timedelta(hours=1),
                                valid_until=now + timedelta(hours=2),
                                model_rule_ids=[(0, 0, {
                                    "model_id": self.category_model.id, "no_create": True})])
        profile.action_schedule()
        self.Profile._cron_apply_schedule()
        self.assertFalse(profile.enforced, "not due yet")
        self.assertTrue(self._as(self.user).has_access("create"))

        profile.valid_from = now - timedelta(minutes=1)
        self.Profile._cron_apply_schedule()
        self.assertTrue(profile.enforced)
        self.assertFalse(self._as(self.user).has_access("create"))

        profile.write({"valid_from": now - timedelta(hours=2),
                       "valid_until": now - timedelta(minutes=1)})
        self.Profile._cron_apply_schedule()
        self.assertFalse(profile.enforced, "an expired profile is suspended automatically")
        self.assertTrue(self._as(self.user).has_access("create"))
