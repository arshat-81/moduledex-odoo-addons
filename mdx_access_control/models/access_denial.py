"""A record of what the profiles actually stopped.

Restrictions that nobody can see working are restrictions nobody trusts. Core
raises every access refusal through two methods on ``ir.access``; hooking them
gives a log of blocked attempts - who, what, which profile - and lets the error
the user sees name the profile instead of leaving them guessing.

The refusal aborts the request, and with it everything written in the request's
transaction. The log row is therefore written on a cursor of its own.
"""

import contextlib
import logging
from datetime import timedelta

from odoo import api, fields, models

from odoo.addons.base.models.ir_access import IN_SELECTION

_logger = logging.getLogger(__name__)

# Repeats inside this window are counted on the existing row, not added: a list
# view refusing forty records must not produce forty lines.
MERGE_SECONDS = 60

OPERATION_LABELS = [
    ("read", "Read"),
    ("write", "Edit"),
    ("create", "Create"),
    ("unlink", "Delete"),
    ("read_only", "Write (read-only access)"),
    ("block_export", "Export"),
    ("block_import", "Import"),
    ("block_api", "API login"),
]


class MdxAccessDenial(models.Model):
    _name = "mdx.access.denial"
    _description = "Blocked Access Attempt"
    _order = "last_seen desc, id desc"
    _rec_name = "summary"

    user_id = fields.Many2one("res.users", string="User", readonly=True, index=True,
                              ondelete="cascade")
    profile_id = fields.Many2one("mdx.access.profile", string="Profile", readonly=True,
                                 index=True, ondelete="set null")
    profile_name = fields.Char(readonly=True)
    model = fields.Char(string="Model", readonly=True, index=True)
    operation = fields.Selection(OPERATION_LABELS, readonly=True, index=True)
    res_ids = fields.Char(string="Records", readonly=True)
    count = fields.Integer(string="Attempts", default=1, readonly=True)
    first_seen = fields.Datetime(readonly=True)
    last_seen = fields.Datetime(readonly=True, index=True)
    summary = fields.Char(compute="_compute_summary")

    @api.depends("user_id", "operation", "model")
    def _compute_summary(self):
        labels = dict(OPERATION_LABELS)
        for denial in self:
            denial.summary = "%s: %s %s" % (
                denial.user_id.name or "?", labels.get(denial.operation, denial.operation or ""),
                denial.model or "")

    @contextlib.contextmanager
    def _log_env(self):
        """An environment whose writes survive the refusal that follows.

        The refusal rolls the request back, so the row needs a cursor of its
        own. Isolated in a method because a test transaction cannot be seen
        from a second cursor, and tests substitute their own.
        """
        with self.env.registry.cursor() as cr:
            yield api.Environment(cr, api.SUPERUSER_ID, {})

    @api.model
    def _log(self, user, model_name, operation, profile_name=None, res_ids=None):
        """Record a blocked attempt. Never raises, never blocks the refusal."""
        try:
            with self._log_env() as env:
                Denial = env["mdx.access.denial"]
                Profile = env["mdx.access.profile"].with_context(active_test=False)
                profile = Profile.search([("name", "=", profile_name)], limit=1) \
                    if profile_name else Profile.browse()
                now = fields.Datetime.now()
                recent = Denial.search([
                    ("user_id", "=", user.id), ("model", "=", model_name),
                    ("operation", "=", operation), ("profile_name", "=", profile_name or False),
                    ("last_seen", ">=", now - timedelta(seconds=MERGE_SECONDS)),
                ], limit=1)
                ids_text = ", ".join(str(i) for i in (res_ids or [])[:20]) or False
                if recent:
                    recent.write({"count": recent.count + 1, "last_seen": now,
                                  "res_ids": ids_text or recent.res_ids})
                else:
                    Denial.create({
                        "user_id": user.id, "profile_id": profile.id,
                        "profile_name": profile_name or False, "model": model_name,
                        "operation": operation, "res_ids": ids_text,
                        "first_seen": now, "last_seen": now,
                    })
        except Exception:  # noqa: BLE001 - the audit row must never mask the refusal
            _logger.exception("mdx_access_control: could not record a blocked attempt")

    @api.autovacuum
    def _gc_denials(self):
        days = self.env["ir.config_parameter"].sudo().get_int(
            "mdx_access_control.denial_retention_days", 180)
        limit = fields.Datetime.now() - timedelta(days=max(1, days))
        self.sudo().search([("last_seen", "<", limit)]).unlink()


class IrAccess(models.Model):
    _inherit = "ir.access"

    def _mdx_blaming_profile(self, model_name, operation, records=None):
        """The profile whose restriction caused a refusal, if one did."""
        user = self.env.user
        Profile = self.env["mdx.access.profile"].sudo()
        if not Profile._rules_snapshot()["profiles"]:
            return False
        if operation != "read" and Profile._policy_profile(user, "read_only"):
            model = self.env.get(model_name)
            if model is not None and not model._transient:
                return Profile._policy_profile(user, "read_only"), "read_only"
        entries = {entry["id"] for entry in Profile._entries_for(user)}
        if not entries:
            return False
        if records:
            failing = self.sudo()._get_failed_accesses(records, operation)
        else:
            failing = self.sudo().search([
                ("model_id.model", "=", model_name), ("group_id", "=", False),
                ("operation", "in", list(IN_SELECTION[operation])),
                ("mdx_profile_id", "in", list(entries)),
            ])
        blamed = failing.filtered(lambda access: access.mdx_profile_id.id in entries)[:1]
        return (blamed.mdx_profile_id.name, operation) if blamed else False

    def _mdx_explain(self, error, model_name, operation, records=None):
        try:
            blame = self._mdx_blaming_profile(model_name, operation, records)
        except Exception:  # noqa: BLE001 - explaining must never replace the real error
            _logger.exception("mdx_access_control: could not attribute an access refusal")
            return error
        if not blame:
            return error
        profile_name, logged_operation = blame
        self.env["mdx.access.denial"]._log(
            self.env.user, model_name, logged_operation, profile_name=profile_name,
            res_ids=records.ids if records else None)
        explained = type(error)("%s\n\n%s" % (error.args[0], self.env._(
            "This is restricted by the access profile \"%s\".", profile_name)))
        if getattr(error, "context", None):
            explained.context = error.context
        return explained

    def _make_model_access_error(self, model_name, operation):
        error = super()._make_model_access_error(model_name, operation)
        return self._mdx_explain(error, model_name, operation)

    def _make_record_access_error(self, records, operation):
        error = super()._make_record_access_error(records, operation)
        return self._mdx_explain(error, records._name, operation, records)
