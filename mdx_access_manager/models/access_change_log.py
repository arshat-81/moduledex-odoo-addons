"""Audit trail for every change to access configuration.

Three core models carry the configuration this module reports on, so all three
are hooked: ``res.groups`` (membership and implication), ``ir.model.access``
(the CRUD grants) and ``ir.rule`` (record rules).

A deliberate trade-off: if writing the log entry itself fails, the exception is
logged and the original write proceeds. Blocking an administrator's security fix
because an audit row could not be inserted is the worse failure of the two, and
a gap in the trail is visible (the row simply is not there) whereas a blocked
security change during an incident is not.
"""

import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Fields worth recording per model. Everything else on these models is
# descriptive text whose history nobody audits.
WATCHED_FIELDS = {
    "res.groups": ("name", "privilege_id", "implied_ids", "user_ids", "comment"),
    "ir.model.access": ("name", "active", "model_id", "group_id",
                        "perm_read", "perm_write", "perm_create", "perm_unlink"),
    "ir.rule": ("name", "active", "model_id", "groups", "domain_force",
                "perm_read", "perm_write", "perm_create", "perm_unlink"),
}


class MdxAccessChangeLog(models.Model):
    _name = "mdx.access.change.log"
    _description = "Access Configuration Change Log"
    _order = "changed_at desc, id desc"
    _rec_name = "description"

    description = fields.Char(required=True, readonly=True)
    res_model = fields.Char(string="Model", required=True, readonly=True, index=True)
    res_id = fields.Integer(string="Record ID", readonly=True, index=True)
    res_name = fields.Char(string="Record", readonly=True)
    operation = fields.Selection(
        [("create", "Created"), ("write", "Changed"), ("unlink", "Deleted")],
        readonly=True, index=True,
    )
    changed_by_id = fields.Many2one("res.users", string="Changed By", readonly=True, index=True)
    changed_at = fields.Datetime(readonly=True, index=True)
    before_json = fields.Text(string="Before", readonly=True)
    after_json = fields.Text(string="After", readonly=True)
    diff_text = fields.Text(string="Change", readonly=True)
    change_set_id = fields.Many2one(
        "mdx.access.change.set", string="From Change Set", readonly=True, ondelete="set null")

    @api.model
    def _audit_enabled(self):
        """Log human changes, not module installs.

        Installing or upgrading any module writes thousands of ir.model.access
        and ir.rule rows. Logging those would bury real changes and slow every
        install, so the same guards core itself uses are applied here:
        ``registry.ready`` / ``registry._init`` (see ir_module.py:599) and the
        ``install_mode`` context key (see ir_model.py:215).
        """
        registry = self.env.registry
        if not registry.ready or registry._init:
            return False
        return not self.env.context.get("install_mode")

    @api.model
    def _record(self, res_model, res_id, description, before=None, after=None,
                operation="write", res_name=None, change_set=None):
        """Insert one audit row. Never raises."""
        if not self._audit_enabled():
            return self.browse()
        try:
            return self.sudo().create({
                "res_model": res_model,
                "res_id": res_id,
                "res_name": res_name or "",
                "description": description,
                "operation": operation,
                "changed_by_id": self.env.uid,
                "changed_at": fields.Datetime.now(),
                "before_json": json.dumps(before or {}, indent=2, sort_keys=True, default=str),
                "after_json": json.dumps(after or {}, indent=2, sort_keys=True, default=str),
                "diff_text": _diff_text(before or {}, after or {}),
                "change_set_id": change_set.id if change_set else False,
            })
        except Exception:
            _logger.exception("mdx_access_manager: could not write the access audit row for %s#%s",
                              res_model, res_id)
            return self.browse()


def _diff_text(before, after):
    """Render only the fields that actually changed.

    A create has an empty ``before``, so every field would otherwise compare
    ``None`` against ``False`` or ``[]`` and print a meaningless "- -> -".
    Comparing the rendered values collapses all the empty spellings into one.
    """
    lines = []
    for key in sorted(set(before) | set(after)):
        old, new = _short(before.get(key)), _short(after.get(key))
        if old != new:
            lines.append("%s: %s -> %s" % (key, old, new))
    return "\n".join(lines)


def _short(value):
    text = "-" if value in (None, False, [], {}) else str(value)
    return text if len(text) <= 200 else text[:197] + "..."


class AccessAuditedMixin(models.AbstractModel):
    """Shared write/create/unlink hooks for the audited core models."""
    _name = "mdx.access.audited.mixin"
    _description = "Audited Access Configuration"

    def _audit_snapshot(self):
        """Readable values of the watched fields, keyed by record id."""
        fnames = WATCHED_FIELDS.get(self._name, ())
        out = {}
        for record in self.sudo():
            values = {}
            for fname in fnames:
                field = self._fields.get(fname)
                if not field:
                    continue
                value = record[fname]
                if field.type in ("many2many", "one2many"):
                    values[fname] = sorted(value.mapped("display_name"))
                elif field.type == "many2one":
                    values[fname] = value.display_name or False
                else:
                    values[fname] = value
            out[record.id] = values
        return out

    def _audit_label(self):
        self.ensure_one()
        return self.sudo().display_name or "%s#%s" % (self._name, self.id)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        Log = self.env["mdx.access.change.log"]
        after = records._audit_snapshot()
        for record in records:
            Log._record(
                res_model=self._name, res_id=record.id, operation="create",
                res_name=record._audit_label(),
                description=self.env._("%s created", self._description),
                after=after.get(record.id, {}),
            )
        return records

    def write(self, vals):
        watched = set(WATCHED_FIELDS.get(self._name, ()))
        if not watched & set(vals):
            return super().write(vals)
        before = self._audit_snapshot()
        result = super().write(vals)
        after = self._audit_snapshot()
        Log = self.env["mdx.access.change.log"]
        for record in self:
            old, new = before.get(record.id, {}), after.get(record.id, {})
            if old == new:
                continue
            Log._record(
                res_model=self._name, res_id=record.id, operation="write",
                res_name=record._audit_label(),
                description=self.env._("%s changed", self._description),
                before=old, after=new,
            )
        return result

    def unlink(self):
        before = self._audit_snapshot()
        labels = {record.id: record._audit_label() for record in self}
        Log = self.env["mdx.access.change.log"]
        # Capture before the rows are gone; the log row itself is written after a
        # successful delete so a failed unlink leaves no misleading entry.
        payload = [(rid, labels.get(rid), before.get(rid, {})) for rid in self.ids]
        result = super().unlink()
        for res_id, label, old in payload:
            Log._record(
                res_model=self._name, res_id=res_id, operation="unlink",
                res_name=label,
                description=self.env._("%s deleted", self._description),
                before=old,
            )
        return result


class ResGroups(models.Model):
    _name = "res.groups"
    _inherit = ["res.groups", "mdx.access.audited.mixin"]


class IrModelAccess(models.Model):
    _name = "ir.model.access"
    _inherit = ["ir.model.access", "mdx.access.audited.mixin"]


class IrRule(models.Model):
    _name = "ir.rule"
    _inherit = ["ir.rule", "mdx.access.audited.mixin"]
