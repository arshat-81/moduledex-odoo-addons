"""Detectors for access configuration that causes access bugs.

Every detector here is decidable from the configuration alone: it either matches
or it does not, with no heuristics and no guessing about intent. That is a
deliberate limit. A detector that reports "this looks too permissive" would be
opinion dressed as a finding, and an administrator who stops trusting the list
stops reading it.

Each detector's rule is stated in its own docstring, together with the core
behaviour that makes it true, so a sceptical reader can check the claim against
the server rather than taking it on faith.
"""

from odoo import api, fields, models
from odoo.exceptions import AccessError

from .access_engine import MODES, MODE_LABELS, modes_of


class MdxAccessFinding(models.Model):
    _name = "mdx.access.finding"
    _description = "Access Configuration Finding"
    _order = "severity, code, id"
    _rec_name = "title"

    code = fields.Char(required=True, readonly=True, index=True)
    title = fields.Char(required=True, readonly=True)
    detail = fields.Text(readonly=True)
    recommendation = fields.Text(readonly=True)
    severity = fields.Selection(
        [("critical", "Critical"), ("warning", "Warning"), ("info", "Information")],
        required=True, readonly=True, index=True,
    )
    status = fields.Selection(
        [("open", "Open"), ("acknowledged", "Acknowledged"), ("resolved", "Resolved")],
        default="open", required=True, index=True,
    )
    group_id = fields.Many2one("res.groups", string="Group", readonly=True, ondelete="cascade")
    model_id = fields.Many2one("ir.model", string="Model", readonly=True, ondelete="cascade")
    # Odoo 20 stores access controls and record rules in one model, ir.access
    access_id = fields.Many2one("ir.access", string="Access Rule", readonly=True,
                                ondelete="cascade")
    detected_at = fields.Datetime(readonly=True)

    _code_target_uniq = models.Constraint(
        "unique(code, group_id, model_id, access_id)",
        "This finding is already recorded for this target.",
    )

    # ------------------------------------------------------------------
    @api.model
    def _check_manager(self):
        if not self.env.user.has_group("mdx_access_manager.group_access_manager"):
            raise AccessError(self.env._(
                "Only Access Rights Manager users can scan the access configuration."
            ))

    @api.model
    def action_scan(self):
        """Re-run every detector.

        Findings already acknowledged or resolved keep their status; anything
        that no longer matches is deleted, so the list reflects the database as
        it is now rather than accumulating history. The audit log is where
        history lives.
        """
        self._check_manager()
        existing = {self._key(f): f for f in self.sudo().search([])}
        seen = set()
        now = fields.Datetime.now()

        for detector in (self._detect_redundant_implication,
                         self._detect_shadowed_permission,
                         self._detect_restriction_shadow,
                         self._detect_admin_path,
                         self._detect_write_without_read):
            for vals in detector():
                vals["detected_at"] = now
                key = self._key_from_vals(vals)
                seen.add(key)
                if key in existing:
                    # refresh the wording, keep the human's triage
                    existing[key].sudo().write({
                        "title": vals["title"],
                        "detail": vals["detail"],
                        "recommendation": vals.get("recommendation"),
                        "severity": vals["severity"],
                        "detected_at": now,
                    })
                else:
                    self.sudo().create(vals)

        stale = [f for key, f in existing.items() if key not in seen]
        if stale:
            self.sudo().browse([f.id for f in stale]).unlink()
        return True

    @api.model
    def _key(self, finding):
        return (finding.code, finding.group_id.id, finding.model_id.id,
                finding.access_id.id)

    @api.model
    def _key_from_vals(self, vals):
        return (vals["code"], vals.get("group_id") or False, vals.get("model_id") or False,
                vals.get("access_id") or False)

    # ------------------------------------------------------------------
    # detectors
    # ------------------------------------------------------------------
    @api.model
    def _detect_redundant_implication(self):
        """A direct implication that another implication already provides.

        If group G implies D directly, and D is also reachable through one of
        G's *other* implied groups, the direct link changes nothing:
        ``all_implied_ids`` is the transitive closure either way. Removing it
        makes the real inheritance shape visible.
        """
        out = []
        groups = self.env["res.groups"].sudo().search([("implied_ids", "!=", False)])
        for group in groups:
            direct = group.implied_ids
            for target in direct:
                others = direct - target
                if not others:
                    continue
                if target in others.all_implied_ids:
                    via = others.filtered(lambda g, t=target: t in g.all_implied_ids)
                    out.append({
                        "code": "redundant_implication",
                        "severity": "info",
                        "group_id": group.id,
                        "title": self.env._(
                            "%(group)s implies %(target)s twice over",
                            group=group.name, target=target.name),
                        "detail": self.env._(
                            "%(group)s lists %(target)s directly, but already reaches it through "
                            "%(via)s. The transitive closure is identical with or without the "
                            "direct link.",
                            group=group.name, target=target.name,
                            via=", ".join(via.mapped("name"))),
                        "recommendation": self.env._(
                            "Remove the direct implication so the inheritance that actually "
                            "matters is the one you can see."),
                    })
        return out

    @api.model
    def _active_accesses_by_model(self):
        by_model = {}
        for access in self.env["ir.access"].sudo().search([("active", "=", True)]):
            by_model.setdefault(access.model_id, []).append(access)
        return by_model

    @api.model
    def _detect_shadowed_permission(self):
        """A permission that grants nothing its own group does not already have.

        ``Model._access_domain`` OR-s together every permission held by the
        user's groups. Holding group G always means holding everything G
        implies, so if an implied group (or G itself, through another row)
        carries an *unconditional* permission for an operation, a second
        permission for that operation on G cannot add a single record. When
        that is true for every operation a row covers, the row is decoration.
        """
        out = []
        for model, accesses in self._active_accesses_by_model().items():
            permissions = [a for a in accesses if a.group_id]
            unconditional = [a for a in permissions if not (a.domain or "").strip()]
            if not unconditional:
                continue
            for access in permissions:
                held = access.group_id.all_implied_ids
                covering = self.env["ir.access"].sudo()
                for mode in modes_of(access.operation):
                    cover = next((
                        other for other in unconditional
                        if other != access
                        and other.group_id in held
                        and mode in modes_of(other.operation)
                        # two identical unconditional rows on one group shadow
                        # each other: report only the later one
                        and not (other.group_id == access.group_id
                                 and not (access.domain or "").strip()
                                 and other.id > access.id)
                    ), None)
                    if cover is None:
                        covering = None
                        break
                    covering |= cover
                if not covering:
                    continue
                out.append({
                    "code": "shadowed_permission",
                    # Information, not a warning: stock Odoo ships dozens of
                    # these, and a list that opens with noise stops being read.
                    "severity": "info",
                    "model_id": model.id,
                    "group_id": access.group_id.id,
                    "access_id": access.id,
                    "title": self.env._(
                        "%(access)s adds nothing on %(model)s",
                        access=access.name, model=model.model),
                    "detail": self.env._(
                        "%(access)s grants %(modes)s to %(group)s, but everyone in that group "
                        "already holds the same operations unconditionally through %(covers)s. "
                        "Permissions are OR-ed, so this row cannot widen or narrow anything.",
                        access=access.name,
                        modes=", ".join(MODE_LABELS[m] for m in modes_of(access.operation)),
                        group=access.group_id.name,
                        covers=", ".join("%s (%s)" % (c.name, c.group_id.name) for c in covering)),
                    "recommendation": self.env._(
                        "Decide which is intended: put the condition on the broader permission, "
                        "or drop this row as redundant."),
                })
        return out

    @api.model
    def _detect_restriction_shadow(self):
        """A restriction sitting alongside conditional permissions on one model.

        ``Model._access_domain`` OR-s the permission domains together and then
        AND-s the result with every restriction (an access rule without a
        group). A restriction can therefore only ever narrow the outcome:
        adding or widening a permission cannot show a record the restriction
        excludes. This is the single most common reason a permission change
        "does nothing".
        """
        out = []
        for model, accesses in self._active_accesses_by_model().items():
            restrictions = [a for a in accesses
                            if not a.group_id and (a.domain or "").strip()]
            conditional = [a for a in accesses
                           if a.group_id and (a.domain or "").strip()]
            if not restrictions or not conditional:
                continue
            for access in restrictions:
                out.append({
                    "code": "restriction_shadow",
                    "severity": "warning",
                    "model_id": model.id,
                    "access_id": access.id,
                    "title": self.env._(
                        "Restriction narrows %(count)s conditional permission(s) on %(model)s",
                        count=len(conditional), model=model.model),
                    "detail": self.env._(
                        "%(access)s has no group (%(modes)s), so its domain is AND-ed with the "
                        "OR of every permission on this model: %(permissions)s. No permission "
                        "can widen access past it.\n\nDomain: %(domain)s",
                        access=access.name,
                        modes=", ".join(MODE_LABELS[m] for m in modes_of(access.operation)) or "-",
                        permissions=", ".join(a.name for a in conditional),
                        domain=access.domain or "[]"),
                    "recommendation": self.env._(
                        "If the permissions are meant to grant wider access, the restriction has "
                        "to be given a group or relaxed."),
                })
        return out

    @api.model
    def _detect_admin_path(self):
        """A group that hands out Settings access through implication.

        ``base.group_system`` bypasses most access controls, so any group whose
        transitive closure contains it is an administrator grant however it is
        named. These are the paths nobody remembers creating.
        """
        admin = self.env.ref("base.group_system", raise_if_not_found=False)
        if not admin:
            return []
        out = []
        for group in self.env["res.groups"].sudo().search([("id", "!=", admin.id)]):
            if admin not in group.all_implied_ids:
                continue
            direct = admin in group.implied_ids
            out.append({
                "code": "admin_path",
                "severity": "critical" if not direct else "warning",
                "group_id": group.id,
                "title": self.env._(
                    "%s grants Settings access", group.name),
                "detail": self.env._(
                    "Holding %(group)s transitively implies %(admin)s, so anyone in it is an "
                    "administrator. The implication is %(kind)s.",
                    group=group.name, admin=admin.name,
                    kind=self.env._("direct") if direct
                    else self.env._("indirect, through another group")),
                "recommendation": self.env._(
                    "Confirm this is intended. An indirect path to Settings is rarely deliberate "
                    "and is invisible on the user form."),
            })
        return out

    @api.model
    def _detect_write_without_read(self):
        """A group that can change a model it cannot read.

        Nothing in the server forbids this, and nothing makes it work either:
        the client cannot show a record it may not read, so the operation
        surfaces as an unexplained access error rather than a missing button.
        """
        out = []
        for model, accesses in self._active_accesses_by_model().items():
            permissions = [a for a in accesses if a.group_id]
            for group in {a.group_id for a in permissions}:
                own = [a for a in permissions if a.group_id == group]
                # Read may legitimately come from an implied group: reading is
                # commonly granted once, on a base role, and only the writes on
                # the specific group.
                held = group.all_implied_ids
                if any("read" in modes_of(a.operation)
                       for a in permissions if a.group_id in held):
                    continue
                writes = [MODE_LABELS[m] for m in ("write", "create", "unlink")
                          if any(m in modes_of(a.operation) for a in own)]
                if not writes:
                    continue
                first = min(own, key=lambda a: a.id)
                out.append({
                    "code": "write_without_read",
                    "severity": "warning",
                    "model_id": model.id,
                    "group_id": group.id,
                    "access_id": first.id,
                    "title": self.env._(
                        "%(group)s can %(modes)s %(model)s without reading it",
                        group=group.name, modes="/".join(writes), model=model.model),
                    "detail": self.env._(
                        "No active permission grants Read on %(model)s to %(group)s or to any "
                        "group it implies, but %(modes)s is granted. Users in this group will hit "
                        "an access error they cannot act on, because the record cannot be "
                        "displayed in the first place.",
                        model=model.model, group=group.name, modes="/".join(writes)),
                    "recommendation": self.env._("Grant Read as well, or withdraw the write access."),
                })
        return out

    # ------------------------------------------------------------------
    def action_acknowledge(self):
        return self.write({"status": "acknowledged"})

    def action_reopen(self):
        return self.write({"status": "open"})

    def action_open_target(self):
        self.ensure_one()
        target = self.access_id or self.group_id or self.model_id
        if not target:
            return False
        return {
            "type": "ir.actions.act_window",
            "res_model": target._name,
            "res_id": target.id,
            "view_mode": "form",
            "target": "current",
        }
