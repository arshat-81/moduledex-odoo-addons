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

from .access_engine import MODES, MODE_LABELS


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
    acl_id = fields.Many2one("ir.model.access", string="Access Control", readonly=True,
                             ondelete="cascade")
    rule_id = fields.Many2one("ir.rule", string="Record Rule", readonly=True, ondelete="cascade")
    detected_at = fields.Datetime(readonly=True)

    _code_target_uniq = models.Constraint(
        "unique(code, group_id, model_id, acl_id, rule_id)",
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
                         self._detect_shadowed_acl,
                         self._detect_global_rule_shadow,
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
                finding.acl_id.id, finding.rule_id.id)

    @api.model
    def _key_from_vals(self, vals):
        return (vals["code"], vals.get("group_id") or False, vals.get("model_id") or False,
                vals.get("acl_id") or False, vals.get("rule_id") or False)

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
    def _detect_shadowed_acl(self):
        """A group access control that grants what everyone already has.

        The server grants an operation when any active row has
        ``group_id IS NULL`` (everyone) or a group the user holds. So once a
        global row grants an operation on a model, every group row granting the
        same operation on that model is decoration - it cannot narrow anything,
        because access control rows only ever add.
        """
        out = []
        rows = self.env["ir.model.access"].sudo().search([("active", "=", True)])
        by_model = {}
        for acl in rows:
            by_model.setdefault(acl.model_id, []).append(acl)
        for model, acls in by_model.items():
            global_acls = [a for a in acls if not a.group_id]
            if not global_acls:
                continue
            for mode in MODES:
                field = "perm_%s" % mode
                if not any(a[field] for a in global_acls):
                    continue
                for acl in acls:
                    if not acl.group_id or not acl[field]:
                        continue
                    out.append({
                        "code": "shadowed_acl",
                        "severity": "warning",
                        "model_id": model.id,
                        "group_id": acl.group_id.id,
                        "acl_id": acl.id,
                        "title": self.env._(
                            "%(mode)s on %(model)s is already global",
                            mode=MODE_LABELS[mode], model=model.model),
                        "detail": self.env._(
                            "%(acl)s grants %(mode)s to %(group)s, but %(globals)s already grants "
                            "it to every user. Restricting the group changes nothing while the "
                            "global row exists.",
                            acl=acl.name, mode=MODE_LABELS[mode],
                            group=acl.group_id.name,
                            globals=", ".join(a.name for a in global_acls if a[field])),
                        "recommendation": self.env._(
                            "Decide which is intended: give the global row a group, or drop the "
                            "group row as redundant."),
                    })
        return out

    @api.model
    def _detect_global_rule_shadow(self):
        """A global record rule sitting alongside group rules on one model.

        ``ir.rule._compute_domain`` OR-s the group rule domains together and
        then AND-s the result with every global rule domain. A global rule can
        therefore only ever narrow the outcome: adding or widening a group rule
        cannot show a record the global rule excludes. This is the single most
        common reason a permission change "does nothing".
        """
        out = []
        rules = self.env["ir.rule"].sudo().search([("active", "=", True)])
        by_model = {}
        for rule in rules:
            by_model.setdefault(rule.model_id, []).append(rule)
        for model, model_rules in by_model.items():
            global_rules = [r for r in model_rules if not r.groups]
            group_rules = [r for r in model_rules if r.groups]
            if not global_rules or not group_rules:
                continue
            for rule in global_rules:
                modes = [MODE_LABELS[m] for m in MODES if rule["perm_%s" % m]]
                out.append({
                    "code": "global_rule_shadow",
                    "severity": "warning",
                    "model_id": model.id,
                    "rule_id": rule.id,
                    "title": self.env._(
                        "Global rule narrows %(count)s group rule(s) on %(model)s",
                        count=len(group_rules), model=model.model),
                    "detail": self.env._(
                        "%(rule)s is global (%(modes)s) and its domain is AND-ed with the OR of "
                        "every group rule on this model: %(groups)s. No group rule can widen "
                        "access past it.\n\nDomain: %(domain)s",
                        rule=rule.name, modes=", ".join(modes) or "-",
                        groups=", ".join(r.name for r in group_rules),
                        domain=rule.domain_force or "[]"),
                    "recommendation": self.env._(
                        "If the group rules are meant to grant wider access, the global rule has "
                        "to be given groups or relaxed."),
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
        rows = self.env["ir.model.access"].sudo().search([("active", "=", True)])
        by_pair = {}
        for acl in rows:
            by_pair.setdefault((acl.model_id, acl.group_id), []).append(acl)
        for (model, group), acls in by_pair.items():
            if not group:
                continue
            can_read = any(a.perm_read for a in acls)
            if can_read:
                continue
            writes = [MODE_LABELS[m] for m in ("write", "create", "unlink")
                      if any(a["perm_%s" % m] for a in acls)]
            if not writes:
                continue
            out.append({
                "code": "write_without_read",
                "severity": "warning",
                "model_id": model.id,
                "group_id": group.id,
                "acl_id": acls[0].id,
                "title": self.env._(
                    "%(group)s can %(modes)s %(model)s without reading it",
                    group=group.name, modes="/".join(writes), model=model.model),
                "detail": self.env._(
                    "No active access control grants Read on %(model)s to %(group)s, but "
                    "%(modes)s is granted. Users in this group will hit an access error they "
                    "cannot act on, because the record cannot be displayed in the first place.",
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
        target = self.acl_id or self.rule_id or self.group_id or self.model_id
        if not target:
            return False
        return {
            "type": "ir.actions.act_window",
            "res_model": target._name,
            "res_id": target.id,
            "view_mode": "form",
            "target": "current",
        }
