"""Compare the effective rights of two users, or of a user against a proposal."""

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .access_engine import MODES, MODE_LABELS


class MdxAccessCompare(models.Model):
    _name = "mdx.access.compare"
    _description = "Access Rights Comparison"
    _order = "create_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True, readonly=True)
    mode = fields.Selection(
        [("users", "Two Users"), ("proposal", "User vs Proposed Groups")],
        default="users", required=True,
        help="Compare two existing users, or compare one user against a hypothetical group set. "
             "Neither comparison writes anything.",
    )
    left_user_id = fields.Many2one(
        "res.users", string="Baseline User", required=True, ondelete="cascade",
        default=lambda self: self.env.user,
    )
    right_user_id = fields.Many2one("res.users", string="Compared User", ondelete="cascade")
    proposed_group_ids = fields.Many2many(
        "res.groups", "mdx_access_compare_group_rel", "compare_id", "group_id",
        string="Proposed Groups",
        help="The group set to resolve instead of the compared user's own groups. Implied groups "
             "are expanded automatically, so list only what you would tick on the user form.",
    )
    model_filter = fields.Char(string="Model Filter")
    include_transient = fields.Boolean(string="Include Wizards")
    only_differences = fields.Boolean(
        string="Only Differences", default=True,
        help="Hide models where both sides resolve identically.",
    )

    line_ids = fields.One2many("mdx.access.compare.line", "compare_id", string="Differences")
    gained_count = fields.Integer(string="Models Gained", readonly=True)
    lost_count = fields.Integer(string="Models Lost", readonly=True)
    same_count = fields.Integer(string="Models Unchanged", readonly=True)
    last_run_at = fields.Datetime(readonly=True)
    state_message = fields.Char(readonly=True)

    @api.depends("mode", "left_user_id", "right_user_id", "proposed_group_ids")
    def _compute_name(self):
        for rec in self:
            left = rec.left_user_id.name or "?"
            if rec.mode == "proposal":
                rec.name = self.env._("%(left)s vs %(n)s proposed group(s)",
                                              left=left, n=len(rec.proposed_group_ids))
            else:
                rec.name = self.env._("%(left)s vs %(right)s", left=left,
                                              right=rec.right_user_id.name or "?")

    def _check_manager(self):
        if not self.env.user.has_group("mdx_access_manager.group_access_manager"):
            raise AccessError(self.env._(
                "Only Access Rights Manager users can compare permissions."
            ))

    def action_run(self):
        self._check_manager()
        Engine = self.env["mdx.access.engine"]
        for rec in self:
            if rec.mode == "users" and not rec.right_user_id:
                raise UserError(self.env._("Choose the user to compare against."))
            if rec.mode == "proposal" and not rec.proposed_group_ids:
                raise UserError(self.env._("List at least one proposed group."))

            rec.line_ids.unlink()

            model_names = rec._model_names()
            _left_explicit, left_groups = Engine._user_group_sets(rec.left_user_id)
            if rec.mode == "users":
                _r_explicit, right_groups = Engine._user_group_sets(rec.right_user_id)
                right_label = rec.right_user_id.name
            else:
                right_groups = Engine._expand_groups(rec.proposed_group_ids)
                right_label = self.env._("proposal")

            left_perms = Engine._perms_by_model(left_groups.ids, model_names=model_names)
            right_perms = Engine._perms_by_model(right_groups.ids, model_names=model_names)

            lines, counts = rec._diff_lines(left_perms, right_perms)
            rec.write({
                "line_ids": lines,
                "gained_count": counts["gained"],
                "lost_count": counts["lost"],
                "same_count": counts["same"],
                "last_run_at": fields.Datetime.now(),
                "state_message": self.env._(
                    "%(gained)s gained, %(lost)s lost, %(same)s unchanged against %(right)s.",
                    gained=counts["gained"], lost=counts["lost"], same=counts["same"],
                    right=right_label),
            })
        return True

    def _model_names(self):
        self.ensure_one()
        if not self.model_filter and self.include_transient:
            return None
        domain = []
        if not self.include_transient:
            domain.append(("transient", "=", False))
        if self.model_filter:
            domain.append(("model", "ilike", self.model_filter.strip()))
        return self.env["ir.model"].sudo().search(domain).mapped("model")

    def _diff_lines(self, left_perms, right_perms):
        """Build one line per model whose resolved operations differ.

        A model missing from a side means no access at all there, which is the
        server's own semantics for a model with no granting access control.
        """
        self.ensure_one()
        empty = {m: False for m in MODES}
        counts = {"gained": 0, "lost": 0, "same": 0}
        model_ids = {
            m.model: m.id
            for m in self.env["ir.model"].sudo().search([
                ("model", "in", list(set(left_perms) | set(right_perms)))])
        }
        lines = []
        for model_name in sorted(set(left_perms) | set(right_perms)):
            left = left_perms.get(model_name, empty)
            right = right_perms.get(model_name, empty)
            gained = [m for m in MODES if right[m] and not left[m]]
            lost = [m for m in MODES if left[m] and not right[m]]
            if gained:
                counts["gained"] += 1
            if lost:
                counts["lost"] += 1
            if not gained and not lost:
                counts["same"] += 1
                if self.only_differences:
                    continue
            model_id = model_ids.get(model_name)
            if not model_id:
                continue
            if gained and lost:
                change = "both"
            elif gained:
                change = "gained"
            elif lost:
                change = "lost"
            else:
                change = "same"
            lines.append((0, 0, {
                "model_id": model_id,
                "change": change,
                "gained_modes": ", ".join(MODE_LABELS[m] for m in gained),
                "lost_modes": ", ".join(MODE_LABELS[m] for m in lost),
                "left_summary": _summary(left),
                "right_summary": _summary(right),
            }))
        return lines, counts


def _summary(perms):
    granted = [MODE_LABELS[m] for m in MODES if perms[m]]
    return " / ".join(granted) or "—"


class MdxAccessCompareLine(models.Model):
    _name = "mdx.access.compare.line"
    _description = "Access Comparison Row"
    _order = "change, model_name, id"
    _rec_name = "model_name"

    compare_id = fields.Many2one("mdx.access.compare", required=True, ondelete="cascade", index=True)
    model_id = fields.Many2one("ir.model", string="Model", required=True, ondelete="cascade")
    model_name = fields.Char(related="model_id.model", store=True, string="Technical Name")
    model_label = fields.Char(related="model_id.name", string="Model")
    change = fields.Selection(
        [("gained", "Gained"), ("lost", "Lost"), ("both", "Gained and Lost"), ("same", "Unchanged")],
        required=True, readonly=True,
    )
    gained_modes = fields.Char(string="Gained", readonly=True)
    lost_modes = fields.Char(string="Lost", readonly=True)
    left_summary = fields.Char(string="Baseline", readonly=True)
    right_summary = fields.Char(string="Compared", readonly=True)
