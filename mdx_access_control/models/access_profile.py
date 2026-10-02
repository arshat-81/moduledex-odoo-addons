"""Access profiles: who is restricted, and the single source every hook reads.

A profile is a named set of restrictions applied to users and/or groups. Two
design decisions shape everything else in this module:

* **Model and record restrictions are native.** Odoo 20 keeps every access rule
  in ``ir.access``; a row without a group is a *restriction*, AND-ed onto
  whatever a user's groups grant. A model rule here is written as exactly such
  a row, with a domain that only bites for the profile's members. The server
  then enforces it on every path there is, and nothing has to be patched.

* **Everything else reads one cached snapshot.** Menu, field and element rules
  are applied per user after core's own cached view and menu computation. Those
  hooks sit on hot paths (``_has_field_access`` runs for every field of every
  write), so they must not query. ``_rules_snapshot`` holds the complete rule
  set in the ormcache; it is user-independent, and membership is resolved from
  the user's group ids, which core already caches.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import frozendict

# What a model rule turns into when the operation is blocked outright.
DOMAIN_NOTHING = "[(0, '=', 1)]"

# policy field on the profile -> the snapshot flag saying any profile uses it
POLICY_FLAGS = {
    "read_only": "has_read_only",
    "block_api": "has_api_block",
    "block_export": "has_export_block",
    "block_import": "has_import_block",
}


class MdxAccessProfile(models.Model):
    _name = "mdx.access.profile"
    _description = "Access Profile"
    _inherit = ["mail.thread"]
    _order = "sequence, name, id"

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    enforced = fields.Boolean(
        string="Enforced", readonly=True, copy=False, tracking=True,
        help="Only an enforced profile restricts anybody. A profile that is not enforced is a "
             "draft: build it, preview it, then enforce it.",
    )
    note = fields.Text(string="Purpose")

    user_ids = fields.Many2many(
        "res.users", "mdx_access_profile_user_rel", "profile_id", "user_id",
        string="Users", tracking=True,
        help="Users this profile restricts directly.",
    )
    group_ids = fields.Many2many(
        "res.groups", "mdx_access_profile_group_rel", "profile_id", "group_id",
        string="Groups", tracking=True,
        help="Everyone holding one of these groups, directly or through another group, is "
             "restricted by this profile.",
    )
    include_admins = fields.Boolean(
        string="Also restrict administrators", tracking=True,
        help="Off by default: users with Settings access are never restricted, so a wrong rule "
             "cannot lock you out. Turn it on only when administrators must be bound too.",
    )
    role_scope = fields.Selection(
        [("light_user", "Every Light user"), ("regular_user", "Every regular User")],
        string="Whole Role",
        help="Odoo 20 sorts internal users into Light, User and Administrator from the groups "
             "they hold. Pick a role to restrict everybody in it, present and future, without "
             "listing anyone.",
    )
    member_count = fields.Integer(compute="_compute_member_count", string="Restricted Users")

    # -- policies: what the members may do at all --------------------------
    read_only = fields.Boolean(
        string="Read-only Access", tracking=True,
        help="Members can look but not touch: create, edit and delete are refused on every "
             "business model, by the server. Their own preferences and saved filters still work.",
    )
    block_api = fields.Boolean(
        string="No API Access", tracking=True,
        help="Members can only use Odoo through the browser. XML-RPC, JSON-RPC and API keys are "
             "refused at authentication, so scripts and integrations cannot act as them.",
    )
    block_export = fields.Boolean(
        string="No Export", tracking=True,
        help="Members cannot export records. Refused by the server, not only hidden.",
    )
    block_import = fields.Boolean(
        string="No Import", tracking=True,
        help="Members cannot import files. Refused by the server.",
    )

    # -- validity window ----------------------------------------------------
    valid_from = fields.Datetime(
        string="Starts On", tracking=True,
        help="With Enforce on Schedule, the profile starts restricting at this moment.")
    valid_until = fields.Datetime(
        string="Ends On", tracking=True,
        help="The profile is suspended automatically at this moment. Use it for a restriction "
             "that must not outlive its reason: an audit, a notice period, a freeze.")
    scheduled = fields.Boolean(
        string="Enforce on Schedule", readonly=True, copy=False, tracking=True)

    menu_ids = fields.Many2many(
        "ir.ui.menu", "mdx_access_profile_menu_rel", "profile_id", "menu_id",
        string="Hidden Menus",
        help="These menus, and everything under them, disappear for the profile's members.",
    )
    model_rule_ids = fields.One2many("mdx.access.model.rule", "profile_id", string="Models", copy=True)
    field_rule_ids = fields.One2many("mdx.access.field.rule", "profile_id", string="Fields", copy=True)
    element_rule_ids = fields.One2many(
        "mdx.access.element.rule", "profile_id", string="Buttons, Tabs, Reports", copy=True)
    access_ids = fields.One2many(
        "ir.access", "mdx_profile_id", string="Generated Restrictions", readonly=True)
    access_count = fields.Integer(compute="_compute_access_count", string="Native Restrictions")

    # ------------------------------------------------------------------
    # membership
    # ------------------------------------------------------------------
    @api.model
    def _system_group_id(self):
        return self.env["ir.model.data"]._xmlid_to_res_id(
            "base.group_system", raise_if_not_found=False) or 0

    def _members(self):
        """The users this profile restricts right now."""
        self.ensure_one()
        users = self.user_ids
        if self.group_ids:
            users |= self.env["res.users"].sudo().with_context(active_test=False).search(
                [("all_group_ids", "in", self.group_ids.ids)])
        if self.role_scope:
            users |= self.env["res.users"].sudo().search([("role", "=", self.role_scope)])
        if not self.include_admins:
            system = self._system_group_id()
            users = users.filtered(lambda user: system not in user._get_group_ids())
        # the superuser bypasses every access rule; listing it would be a lie
        return users.filtered(lambda user: not user._is_superuser())

    @api.depends("user_ids", "group_ids", "role_scope", "include_admins")
    def _compute_member_count(self):
        for profile in self:
            profile.member_count = len(profile.sudo()._members())

    @api.depends("access_ids")
    def _compute_access_count(self):
        for profile in self:
            profile.access_count = len(profile.sudo().access_ids)

    def _membership_expression(self):
        """Python expression, true when ``user`` is a member of this profile.

        Evaluated by core inside an ``ir.access`` domain, where ``user`` is the
        current user. It mirrors ``_matches`` below exactly; the two must
        never disagree.
        """
        self.ensure_one()
        parts = []
        if self.user_ids:
            parts.append("user.id in %r" % (tuple(sorted(self.user_ids.ids)),))
        if self.group_ids:
            parts.append("not set(%r).isdisjoint(user.all_group_ids.ids)"
                         % (tuple(sorted(self.group_ids.ids)),))
        if self.role_scope:
            parts.append("user.role == %r" % self.role_scope)
        if not parts:
            return ""
        expression = " or ".join(parts)
        if not self.include_admins and self._system_group_id():
            expression = "(%s) and %d not in user.all_group_ids.ids" % (
                expression, self._system_group_id())
        return expression

    # ------------------------------------------------------------------
    # the cached rule set
    # ------------------------------------------------------------------
    @api.model
    @api.ormcache()
    def _rules_snapshot(self):
        """Every enforced rule, in a shape the hooks can use without a query."""
        profiles = self.sudo().search([("enforced", "=", True)])
        Menu = self.env["ir.ui.menu"].sudo().with_context(active_test=False)
        entries = []
        ui_models = set()
        strict_models = set()
        binding_models = set()
        for profile in profiles:
            if not (profile.user_ids or profile.group_ids or profile.role_scope):
                continue
            field_rules = {}
            for rule in profile.field_rule_ids:
                if not rule.field_id:
                    continue
                per_model = field_rules.setdefault(rule.model_name, {})
                previous = per_model.get(rule.field_name, (False, False, False, False))
                # two rules on one field add up, they do not override each other
                per_model[rule.field_name] = (
                    previous[0] or rule.invisible,
                    previous[1] or rule.readonly,
                    previous[2] or rule.required,
                    previous[3] or rule.strict,
                )
                ui_models.add(rule.model_name)
                if rule.strict:
                    strict_models.add(rule.model_name)

            elements = {}
            for rule in profile.element_rule_ids:
                per_model = elements.setdefault(rule.model_name, {})
                per_model.setdefault(rule.kind, set()).add(rule.name)
                if rule.kind in ("report", "action"):
                    binding_models.add(rule.model_name)
                else:
                    ui_models.add(rule.model_name)
            for rule in profile.model_rule_ids.filtered("hide_chatter"):
                elements.setdefault(rule.model_name, {}).setdefault("chatter", set()).add("chatter")
                ui_models.add(rule.model_name)

            hidden_menus = frozenset(
                Menu.search([("id", "child_of", profile.menu_ids.ids)]).ids
            ) if profile.menu_ids else frozenset()

            entries.append(frozendict({
                "id": profile.id,
                "users": frozenset(profile.user_ids.ids),
                "groups": frozenset(profile.group_ids.ids),
                "role": profile.role_scope or False,
                "name": profile.name,
                "include_admins": profile.include_admins,
                "read_only": profile.read_only,
                "block_api": profile.block_api,
                "block_export": profile.block_export,
                "block_import": profile.block_import,
                "menus": hidden_menus,
                "fields": frozendict({
                    model: frozendict(rules) for model, rules in field_rules.items()}),
                "elements": frozendict({
                    model: frozendict({kind: frozenset(names) for kind, names in kinds.items()})
                    for model, kinds in elements.items()}),
            }))
        return frozendict({
            "profiles": tuple(entries),
            "system_group": self._system_group_id(),
            "user_group": self._group_id("base.group_user"),
            "regular_group": self._group_id("base.group_user_regular"),
            "has_read_only": any(entry["read_only"] for entry in entries),
            "has_api_block": any(entry["block_api"] for entry in entries),
            "has_export_block": any(entry["block_export"] for entry in entries),
            "has_import_block": any(entry["block_import"] for entry in entries),
            "ui_models": frozenset(ui_models),
            "strict_models": frozenset(strict_models),
            "binding_models": frozenset(binding_models),
            "has_menus": any(entry["menus"] for entry in entries),
        })

    @api.model
    def _group_id(self, xmlid):
        return self.env["ir.model.data"]._xmlid_to_res_id(xmlid, raise_if_not_found=False) or 0

    @api.model
    def _role_of(self, group_ids, snapshot):
        """The Odoo 20 role tier, derived from group ids alone.

        Same answer as ``res.users.role`` without computing it: every group
        that is not a light one implies ``base.group_user_regular``, so holding
        that group is what "regular" means.
        """
        if snapshot["system_group"] in group_ids:
            return "group_system"
        if snapshot["user_group"] not in group_ids:
            return False
        return "regular_user" if snapshot["regular_group"] in group_ids else "light_user"

    @api.model
    def _matches(self, entry, user, group_ids, snapshot):
        member = user.id in entry["users"] or not entry["groups"].isdisjoint(group_ids)
        if not member and entry["role"]:
            member = self._role_of(group_ids, snapshot) == entry["role"]
        if not member:
            return False
        return entry["include_admins"] or snapshot["system_group"] not in group_ids

    @api.model
    def _entries_for(self, user):
        """Snapshot entries of the enforced profiles that restrict ``user``."""
        snapshot = self._rules_snapshot()
        if not snapshot["profiles"] or user._is_superuser():
            return ()
        group_ids = frozenset(user._get_group_ids())
        return tuple(
            entry for entry in snapshot["profiles"]
            if self._matches(entry, user, group_ids, snapshot)
        )

    @api.model
    def _policy_profile(self, user, policy):
        """Name of an enforced profile applying ``policy`` to the user, or False."""
        if not self._rules_snapshot()[POLICY_FLAGS[policy]]:
            return False
        for entry in self._entries_for(user):
            if entry[policy]:
                return entry["name"]
        return False

    @api.model
    def _hidden_menu_ids(self, user):
        if not self._rules_snapshot()["has_menus"]:
            return frozenset()
        hidden = set()
        for entry in self._entries_for(user):
            hidden |= entry["menus"]
        return frozenset(hidden)

    @api.model
    def _field_rules(self, user, model_name):
        """``{field_name: (invisible, readonly, required, strict)}`` for the user."""
        merged = {}
        for entry in self._entries_for(user):
            for name, flags in entry["fields"].get(model_name, {}).items():
                previous = merged.get(name, (False, False, False, False))
                merged[name] = tuple(a or b for a, b in zip(previous, flags))
        return merged

    @api.model
    def _element_rules(self, user, model_name):
        """``{kind: set(names)}`` of the elements hidden for the user."""
        merged = {}
        for entry in self._entries_for(user):
            for kind, names in entry["elements"].get(model_name, {}).items():
                merged.setdefault(kind, set()).update(names)
        return merged

    # ------------------------------------------------------------------
    # keeping enforcement in step with the configuration
    # ------------------------------------------------------------------
    def _refresh_enforcement(self):
        """Rebuild the native restrictions and drop every cached answer."""
        self.sudo()._sync_access_rows()
        self._invalidate_enforcement()

    @api.model
    def _invalidate_enforcement(self):
        # the rule snapshot, and the menus core caches per user
        self.env.transaction.invalidate_ormcache()
        # access domains are cached per user and operation
        self.env["ir.access"]._clear_caches()

    def _sync_access_rows(self):
        """Make ``ir.access`` say exactly what the model rules say.

        Rows are replaced rather than patched: the set is small, and a row
        whose domain no longer matches its rule is worse than a brief rewrite.
        """
        Access = self.env["ir.access"].sudo()
        for profile in self:
            wanted = []
            expression = profile._membership_expression() if profile.enforced else ""
            if expression and profile.active:
                for rule in profile.model_rule_ids:
                    wanted.extend(rule._access_row_values(expression))
            existing = Access.with_context(active_test=False).search(
                [("mdx_profile_id", "=", profile.id)])
            current = {
                (row.mdx_model_rule_id.id, row.operation, row.domain or "", row.name): row
                for row in existing
            }
            target = {
                (vals["mdx_model_rule_id"], vals["operation"], vals["domain"], vals["name"]): vals
                for vals in wanted
            }
            obsolete = [row.id for key, row in current.items() if key not in target]
            if obsolete:
                Access.browse(obsolete).unlink()
            missing = [vals for key, vals in target.items() if key not in current]
            if missing:
                Access.create(missing)

    @api.model_create_multi
    def create(self, vals_list):
        profiles = super().create(vals_list)
        profiles._refresh_enforcement()
        return profiles

    def write(self, vals):
        result = super().write(vals)
        self._refresh_enforcement()
        return result

    def unlink(self):
        self.sudo().access_ids.unlink()
        result = super().unlink()
        self._invalidate_enforcement()
        return result

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def action_enforce(self):
        for profile in self:
            profile._check_enforceable()
        self.write({"enforced": True, "scheduled": False})
        return True

    def _check_enforceable(self):
        self.ensure_one()
        if not (self.user_ids or self.group_ids or self.role_scope):
            raise UserError(self.env._(
                "Assign %s to at least one user, group or role before enforcing it.", self.name))
        if not (self.menu_ids or self.model_rule_ids or self.field_rule_ids
                or self.element_rule_ids or self.read_only or self.block_api
                or self.block_export or self.block_import):
            raise UserError(self.env._("%s has no restriction to enforce yet.", self.name))

    def action_suspend(self):
        self.write({"enforced": False, "scheduled": False})
        return True

    def action_schedule(self):
        """Arm the profile: the scheduler enforces it at Starts On."""
        for profile in self:
            profile._check_enforceable()
            if not profile.valid_from:
                raise UserError(self.env._(
                    "Set Starts On to schedule %s, or enforce it now.", profile.name))
        self.write({"scheduled": True})
        return True

    @api.constrains("valid_from", "valid_until")
    def _check_window(self):
        for profile in self:
            if profile.valid_from and profile.valid_until \
                    and profile.valid_until <= profile.valid_from:
                raise UserError(self.env._("Ends On must be after Starts On."))

    @api.model
    def _cron_apply_schedule(self):
        """Start the profiles that are due and stop the ones that have expired."""
        now = fields.Datetime.now()
        expired = self.search([("enforced", "=", True), ("valid_until", "!=", False),
                               ("valid_until", "<=", now)])
        for profile in expired:
            profile.write({"enforced": False, "scheduled": False})
            profile.message_post(body=self.env._("Suspended automatically: it reached its end date."))
        due = self.search([("scheduled", "=", True), ("enforced", "=", False),
                           ("valid_from", "<=", now)])
        for profile in due:
            if profile.valid_until and profile.valid_until <= now:
                profile.scheduled = False
                continue
            profile.write({"enforced": True, "scheduled": False})
            profile.message_post(body=self.env._("Enforced automatically: it reached its start date."))
        return True

    def action_preview(self):
        self.ensure_one()
        members = self.sudo()._members()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Preview"),
            "res_model": "mdx.access.preview",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_profile_id": self.id,
                "default_user_id": members[:1].id,
            },
        }

    def action_pick_elements(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Pick Buttons, Tabs, Reports"),
            "res_model": "mdx.access.element.picker",
            "view_mode": "form",
            "target": "new",
            "context": {"default_profile_id": self.id},
        }

    def action_view_access(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Native Restrictions"),
            "res_model": "ir.access",
            "view_mode": "list,form",
            "domain": [("mdx_profile_id", "=", self.id)],
            "context": {"active_test": False, "create": False},
        }

    def action_view_members(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Restricted Users"),
            "res_model": "res.users",
            "view_mode": "list,form",
            "domain": [("id", "in", self.sudo()._members().ids)],
            "context": {"create": False},
        }
