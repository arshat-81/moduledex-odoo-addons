"""Staged group changes: preview the effect, then apply or discard.

The preview is pure computation. Because the engine resolves access from a group
set rather than from a stored user, the hypothetical result is obtained by
expanding ``current groups + additions - removals`` and resolving that — nothing
is written, no savepoint is opened, and an aborted preview cannot leave state
behind. Applying is the only write, it goes through ``res.users`` normally so
every core constraint (disjoint groups in particular) still fires, and it is
recorded in the audit trail.
"""

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .access_engine import MODES, MODE_LABELS


class MdxAccessChangeSet(models.Model):
    _name = "mdx.access.change.set"
    _description = "Staged Access Change"
    _order = "create_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True, readonly=True)
    user_id = fields.Many2one(
        "res.users", string="User", required=True, ondelete="cascade",
        help="The user whose group assignment would change.",
    )
    add_group_ids = fields.Many2many(
        "res.groups", "mdx_access_change_add_rel", "change_id", "group_id",
        string="Groups to Add",
    )
    remove_group_ids = fields.Many2many(
        "res.groups", "mdx_access_change_remove_rel", "change_id", "group_id",
        string="Groups to Remove",
        help="Only groups assigned directly on the user form can be removed. A group the user "
             "holds because another group implies it has to be removed at its source.",
    )
    state = fields.Selection(
        [("draft", "Draft"), ("previewed", "Previewed"), ("applied", "Applied"),
         ("discarded", "Discarded")],
        default="draft", required=True, readonly=True,
    )
    line_ids = fields.One2many("mdx.access.change.line", "change_id", string="Impact")
    gained_count = fields.Integer(string="Permissions Gained", readonly=True)
    lost_count = fields.Integer(string="Permissions Lost", readonly=True)
    warning = fields.Text(readonly=True)
    previewed_at = fields.Datetime(readonly=True)
    applied_at = fields.Datetime(readonly=True)
    applied_by_id = fields.Many2one("res.users", string="Applied By", readonly=True)

    @api.depends("user_id", "add_group_ids", "remove_group_ids")
    def _compute_name(self):
        for rec in self:
            # "+1 / -0" meant nothing to anyone reading a list of these
            parts = []
            if rec.add_group_ids:
                parts.append(self.env._("+%s group(s)", len(rec.add_group_ids)))
            if rec.remove_group_ids:
                parts.append(self.env._("-%s group(s)", len(rec.remove_group_ids)))
            rec.name = "%s: %s" % (rec.user_id.name or "?",
                                   ", ".join(parts) or self.env._("nothing staged"))

    def _check_manager(self):
        if not self.env.user.has_group("mdx_access_manager.group_access_manager"):
            raise AccessError(self.env._(
                "Only Access Rights Manager users can stage permission changes."
            ))

    @api.constrains("add_group_ids", "remove_group_ids")
    def _check_not_both(self):
        for rec in self:
            overlap = rec.add_group_ids & rec.remove_group_ids
            if overlap:
                raise UserError(self.env._(
                    "A group cannot be added and removed in the same change: %s",
                    ", ".join(overlap.mapped("name")),
                ))

    # ------------------------------------------------------------------
    def _proposed_explicit_groups(self):
        """The group set that would sit on the user form after applying."""
        self.ensure_one()
        current = self.user_id.sudo().group_ids
        return (current | self.add_group_ids) - self.remove_group_ids

    def action_preview(self):
        self._check_manager()
        Engine = self.env["mdx.access.engine"]
        for rec in self:
            if not rec.add_group_ids and not rec.remove_group_ids:
                raise UserError(self.env._("Stage at least one group to add or remove."))
            rec.line_ids.unlink()

            explicit, before_groups = Engine._user_group_sets(rec.user_id)
            proposed_explicit = rec._proposed_explicit_groups()
            after_groups = Engine._expand_groups(proposed_explicit)

            before = Engine._perms_by_model(before_groups.ids)
            after = Engine._perms_by_model(after_groups.ids)

            lines, counts = rec._impact_lines(before, after)
            rec.write({
                "line_ids": lines,
                "gained_count": counts["gained"],
                "lost_count": counts["lost"],
                "state": "previewed",
                "previewed_at": fields.Datetime.now(),
                "warning": rec._build_warning(explicit, before_groups, after_groups),
            })
        return True

    def _impact_lines(self, before, after):
        self.ensure_one()
        empty = {m: False for m in MODES}
        counts = {"gained": 0, "lost": 0}
        names = set(before) | set(after)
        model_ids = {
            m.model: m.id
            for m in self.env["ir.model"].sudo().search([("model", "in", list(names))])
        }
        lines = []
        for model_name in sorted(names):
            b = before.get(model_name, empty)
            a = after.get(model_name, empty)
            gained = [m for m in MODES if a[m] and not b[m]]
            lost = [m for m in MODES if b[m] and not a[m]]
            if not gained and not lost:
                continue
            if gained:
                counts["gained"] += 1
            if lost:
                counts["lost"] += 1
            model_id = model_ids.get(model_name)
            if not model_id:
                continue
            lines.append((0, 0, {
                "model_id": model_id,
                "change": "gained" if gained and not lost else (
                    "lost" if lost and not gained else "both"),
                "gained_modes": ", ".join(MODE_LABELS[m] for m in gained),
                "lost_modes": ", ".join(MODE_LABELS[m] for m in lost),
            }))
        return lines, counts

    def _build_warning(self, explicit, before_groups, after_groups):
        """Flag the things that surprise people, before they click Apply."""
        self.ensure_one()
        notes = []

        not_direct = self.remove_group_ids - explicit
        if not_direct:
            notes.append(self.env._(
                "These groups are not assigned directly and cannot be removed here; they come "
                "from another group that implies them: %s",
                ", ".join(not_direct.mapped("name")),
            ))

        pulled_in = (after_groups - before_groups) - self.add_group_ids
        if pulled_in:
            notes.append(self.env._(
                "The added groups also imply: %s",
                ", ".join(pulled_in.mapped("name")),
            ))

        still_held = self.remove_group_ids & after_groups
        if still_held:
            notes.append(self.env._(
                "Still held after this change, because another assigned group implies them: %s",
                ", ".join(still_held.mapped("name")),
            ))

        admin = self.env.ref("base.group_system", raise_if_not_found=False)
        if admin and admin in after_groups and admin not in before_groups:
            notes.append(self.env._(
                "This change grants Settings access, which bypasses most access controls."
            ))
        return "\n".join(notes)

    # ------------------------------------------------------------------
    def action_apply(self):
        self._check_manager()
        for rec in self:
            if rec.state != "previewed":
                raise UserError(self.env._(
                    "Preview the change before applying it, so the impact is on record."
                ))
            before = rec.user_id.sudo().group_ids
            proposed = rec._proposed_explicit_groups()
            # Deliberately NOT sudo. res.users sets _allow_sudo_commands = False,
            # so an x2many command written through sudo is silently downgraded to
            # the transaction's origin user (orm/fields_relational.py
            # _check_sudo_commands) - it does not raise. Writing as the acting
            # user instead means the change is subject to that user's own rights
            # and every core constraint, disjoint groups included. A module that
            # audits privilege must not hand out privilege of its own.
            rec.user_id.write({"group_ids": [(6, 0, proposed.ids)]})
            user = rec.user_id.sudo()
            self.env["mdx.access.change.log"].sudo()._record(
                res_model="res.users",
                res_id=user.id,
                description=self.env._("Group assignment changed by access change set"),
                before={"group_ids": sorted(before.mapped("name"))},
                after={"group_ids": sorted(proposed.mapped("name"))},
                change_set=rec,
            )
            rec.write({
                "state": "applied",
                "applied_at": fields.Datetime.now(),
                "applied_by_id": self.env.user.id,
            })
        return True

    def action_discard(self):
        self._check_manager()
        self.write({"state": "discarded"})
        return True

    def action_reset(self):
        self._check_manager()
        for rec in self:
            if rec.state == "applied":
                raise UserError(self.env._("An applied change cannot be reopened."))
            rec.line_ids.unlink()
            rec.write({"state": "draft", "warning": False,
                       "gained_count": 0, "lost_count": 0, "previewed_at": False})
        return True


class MdxAccessChangeLine(models.Model):
    _name = "mdx.access.change.line"
    _description = "Staged Access Change Impact"
    _order = "change, model_name, id"
    _rec_name = "model_name"

    change_id = fields.Many2one("mdx.access.change.set", string="Change Set", required=True, ondelete="cascade", index=True)
    model_id = fields.Many2one("ir.model", string="Model", required=True, ondelete="cascade")
    model_name = fields.Char(related="model_id.model", store=True, string="Technical Name")
    model_label = fields.Char(related="model_id.name", string="Model Name")
    change = fields.Selection(
        [("gained", "Gained"), ("lost", "Lost"), ("both", "Gained and Lost")],
        required=True, readonly=True,
    )
    gained_modes = fields.Char(string="Gained", readonly=True)
    lost_modes = fields.Char(string="Lost", readonly=True)
