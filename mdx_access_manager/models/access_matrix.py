"""Effective rights matrix and the provenance drill-down."""

from odoo import api, fields, models
from odoo.exceptions import AccessError

from .access_engine import MODES, MODE_LABELS


class MdxAccessMatrix(models.Model):
    _name = "mdx.access.matrix"
    _description = "Effective Access Rights Matrix"
    _order = "create_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True, readonly=True)
    user_id = fields.Many2one(
        "res.users", string="User", required=True, ondelete="cascade",
        default=lambda self: self.env.user,
        help="The matrix is resolved for this user's groups, including every implied group.",
    )
    model_filter = fields.Char(
        string="Model Filter",
        help="Optional substring matched against the technical model name, e.g. 'account.' "
             "Leave empty to resolve every model.",
    )
    include_transient = fields.Boolean(
        string="Include Wizards",
        help="Transient models (wizards) are excluded by default: they are numerous and "
             "rarely the subject of an access question.",
    )
    line_ids = fields.One2many("mdx.access.matrix.line", "matrix_id", string="Models")

    explicit_group_ids = fields.Many2many(
        "res.groups", "mdx_access_matrix_explicit_rel", "matrix_id", "group_id",
        string="Assigned Groups", readonly=True,
        help="Groups assigned on the user form.",
    )
    effective_group_ids = fields.Many2many(
        "res.groups", "mdx_access_matrix_effective_rel", "matrix_id", "group_id",
        string="Effective Groups", readonly=True,
        help="Assigned groups plus every group they imply, transitively. This is the set the "
             "server actually resolves access against.",
    )
    inherited_group_count = fields.Integer(readonly=True)

    model_count = fields.Integer(string="Models Reachable", readonly=True)
    # Labelled to match the list columns below. The ORM would otherwise render
    # unlink_count as "Unlink Count" while the column beside it says "Delete".
    read_count = fields.Integer(string="Can Read", readonly=True)
    write_count = fields.Integer(string="Can Write", readonly=True)
    create_count = fields.Integer(string="Can Create", readonly=True)
    unlink_count = fields.Integer(string="Can Delete", readonly=True)
    is_superuser = fields.Boolean(readonly=True)
    last_run_at = fields.Datetime(readonly=True)
    state_message = fields.Char(readonly=True)

    @api.depends("user_id", "last_run_at")
    def _compute_name(self):
        for matrix in self:
            if matrix.user_id:
                matrix.name = self.env._("Access of %s", matrix.user_id.name)
            else:
                matrix.name = self.env._("Access Matrix")

    def _check_manager(self):
        if not self.env.user.has_group("mdx_access_manager.group_access_manager"):
            raise AccessError(self.env._(
                "Only Access Rights Manager users can resolve another user's permissions."
            ))

    # ------------------------------------------------------------------
    def action_run(self):
        self._check_manager()
        Engine = self.env["mdx.access.engine"]
        for matrix in self:
            matrix.line_ids.unlink()
            user = matrix.user_id.sudo()
            explicit, effective = Engine._user_group_sets(user)

            model_names = None
            if matrix.model_filter or not matrix.include_transient:
                domain = []
                if not matrix.include_transient:
                    domain.append(("transient", "=", False))
                if matrix.model_filter:
                    domain.append(("model", "ilike", matrix.model_filter.strip()))
                model_names = self.env["ir.model"].sudo().search(domain).mapped("model")
                if not model_names:
                    matrix.write({
                        "explicit_group_ids": [(6, 0, explicit.ids)],
                        "effective_group_ids": [(6, 0, effective.ids)],
                        "inherited_group_count": len(effective) - len(explicit),
                        "model_count": 0, "read_count": 0, "write_count": 0,
                        "create_count": 0, "unlink_count": 0,
                        "is_superuser": user._is_superuser(),
                        "last_run_at": fields.Datetime.now(),
                        "state_message": self.env._("No model matched the filter."),
                    })
                    continue

            perms = Engine._perms_by_model(effective.ids, model_names=model_names)

            models_by_name = {
                m.model: m.id
                for m in self.env["ir.model"].sudo().search([("model", "in", list(perms))])
            }
            lines = []
            for model_name, modes in sorted(perms.items()):
                model_id = models_by_name.get(model_name)
                if not model_id:
                    # A row in ir_model_access whose ir_model was removed without
                    # cleanup. Report it rather than silently dropping it.
                    continue
                lines.append((0, 0, {
                    "model_id": model_id,
                    "can_read": modes["read"],
                    "can_write": modes["write"],
                    "can_create": modes["create"],
                    "can_unlink": modes["unlink"],
                }))

            counts = {m: sum(1 for v in perms.values() if v[m]) for m in MODES}
            note = self.env._("Resolved %(models)s model(s) from %(groups)s effective group(s).",
                              models=len(perms), groups=len(effective))
            foreign = Engine._foreign_enforcement_note()
            if foreign:
                note = "%s %s" % (note, foreign)
            if user._is_superuser():
                note = self.env._(
                    "This user is a superuser: the server bypasses access controls and record "
                    "rules entirely, so the matrix below is what the configuration says, not a "
                    "limit that would be enforced."
                )
            matrix.write({
                "line_ids": lines,
                "explicit_group_ids": [(6, 0, explicit.ids)],
                "effective_group_ids": [(6, 0, effective.ids)],
                "inherited_group_count": len(effective) - len(explicit),
                "model_count": len(perms),
                "read_count": counts["read"],
                "write_count": counts["write"],
                "create_count": counts["create"],
                "unlink_count": counts["unlink"],
                "is_superuser": user._is_superuser(),
                "last_run_at": fields.Datetime.now(),
                "state_message": note,
            })
        return True

    def action_open_user(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "res.users",
            "res_id": self.user_id.id,
            "view_mode": "form",
            "target": "current",
        }


class MdxAccessMatrixLine(models.Model):
    _name = "mdx.access.matrix.line"
    _description = "Effective Access Rights Row"
    _order = "model_name, id"
    _rec_name = "model_name"

    matrix_id = fields.Many2one("mdx.access.matrix", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one(related="matrix_id.user_id", store=True, index=True)
    model_id = fields.Many2one("ir.model", string="Model", required=True, ondelete="cascade")
    model_name = fields.Char(related="model_id.model", store=True, string="Technical Name")
    model_label = fields.Char(related="model_id.name", string="Model")

    can_read = fields.Boolean(string="Read", readonly=True)
    can_write = fields.Boolean(string="Write", readonly=True)
    can_create = fields.Boolean(string="Create", readonly=True)
    can_unlink = fields.Boolean(string="Delete", readonly=True)
    perm_summary = fields.Char(compute="_compute_perm_summary", store=True, string="Operations")

    provenance_ids = fields.One2many("mdx.access.provenance", "line_id", string="Why")
    provenance_count = fields.Integer(compute="_compute_provenance_count")
    rule_note = fields.Char(readonly=True, string="Record Rules")
    has_global_rule = fields.Boolean(readonly=True)

    @api.depends("can_read", "can_write", "can_create", "can_unlink")
    def _compute_perm_summary(self):
        for line in self:
            granted = [MODE_LABELS[m] for m in MODES if line["can_%s" % m]]
            line.perm_summary = " / ".join(granted) or self.env._("None")

    @api.depends("provenance_ids")
    def _compute_provenance_count(self):
        for line in self:
            line.provenance_count = len(line.provenance_ids)

    def action_explain(self):
        """Resolve why this row is granted.

        Provenance is computed here rather than during ``action_run`` because a
        full matrix is thousands of rows and the question is always asked about
        one of them.
        """
        Engine = self.env["mdx.access.engine"]
        Provenance = self.env["mdx.access.provenance"]
        for line in self:
            line.provenance_ids.unlink()
            explicit, effective = Engine._user_group_sets(line.matrix_id.user_id)
            values = []
            for entry in Engine._granting_acls(line.model_name, effective.ids):
                group = entry["group"]
                paths = Engine._implication_paths(explicit, group) if group else []
                values.append({
                    "line_id": line.id,
                    "source": "acl",
                    "group_id": group.id or False,
                    "group_label": Engine._group_label(group) if group else self.env._(
                        "Everyone (global access control)"),
                    "via": " | ".join(paths) if paths else (
                        self.env._("Global — applies to every user") if not group
                        else self.env._("Group is not reachable from this user")),
                    "acl_id": entry["acl"].id,
                    "modes": ", ".join(MODE_LABELS[m] for m in entry["modes"]),
                })

            global_note = []
            for mode in MODES:
                global_rules, group_rules = Engine._rules_for(line.model_name, effective.ids, mode)
                for rule in global_rules:
                    values.append({
                        "line_id": line.id,
                        "source": "global_rule",
                        "group_label": self.env._("Global record rule"),
                        "via": self.env._(
                            "AND-ed with every other rule, so this one always narrows the result"),
                        "rule_id": rule.id,
                        "modes": MODE_LABELS[mode],
                        "domain_text": rule.domain_force or "[]",
                    })
                for rule in group_rules:
                    matched = rule.groups & effective
                    values.append({
                        "line_id": line.id,
                        "source": "group_rule",
                        "group_id": matched[:1].id or False,
                        "group_label": Engine._group_label(matched[:1]),
                        "via": self.env._("OR-ed with the user's other group rules"),
                        "rule_id": rule.id,
                        "modes": MODE_LABELS[mode],
                        "domain_text": rule.domain_force or "[]",
                    })
                if global_rules:
                    global_note.append(MODE_LABELS[mode])

            # de-duplicate rule rows that repeat across modes
            seen = set()
            deduped = []
            for vals in values:
                key = (vals.get("source"), vals.get("acl_id"), vals.get("rule_id"),
                       vals.get("group_id"))
                if key in seen and vals.get("source") in ("global_rule", "group_rule"):
                    for existing in deduped:
                        ekey = (existing.get("source"), existing.get("acl_id"),
                                existing.get("rule_id"), existing.get("group_id"))
                        if ekey == key:
                            existing["modes"] = "%s, %s" % (existing["modes"], vals["modes"])
                            break
                    continue
                seen.add(key)
                deduped.append(vals)

            Provenance.create(deduped)
            rule_total = sum(1 for v in deduped if v["source"] in ("global_rule", "group_rule"))
            line.write({
                "rule_note": self.env._("%s rule(s) apply", rule_total) if rule_total
                else self.env._("No record rules — all records visible"),
                "has_global_rule": bool(global_note),
            })
        return True


class MdxAccessProvenance(models.Model):
    _name = "mdx.access.provenance"
    _description = "Why Access Is Granted"
    _order = "source, id"

    line_id = fields.Many2one("mdx.access.matrix.line", required=True, ondelete="cascade", index=True)
    source = fields.Selection(
        [("acl", "Access Control"), ("group_rule", "Group Rule"),
         ("global_rule", "Global Rule")],
        required=True,
    )
    group_id = fields.Many2one("res.groups", string="Group", ondelete="cascade")
    group_label = fields.Char(string="Group", readonly=True)
    via = fields.Char(string="Path", readonly=True)
    modes = fields.Char(string="Operations", readonly=True)
    acl_id = fields.Many2one("ir.model.access", string="Access Control", ondelete="cascade")
    rule_id = fields.Many2one("ir.rule", string="Record Rule", ondelete="cascade")
    domain_text = fields.Char(string="Domain", readonly=True)
