"""A write an agent asked for, waiting on a human."""

import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MdxMcpApproval(models.Model):
    _name = "mdx.mcp.approval"
    _description = "MCP Write Approval"
    _order = "create_date desc, id desc"
    _rec_name = "summary"

    summary = fields.Char(required=True, readonly=True)
    tool_id = fields.Many2one("mdx.mcp.tool", required=True, readonly=True, ondelete="cascade")
    requested_by_id = fields.Many2one("res.users", string="Requested As", readonly=True)
    client_name = fields.Char(readonly=True)
    model_name = fields.Char(string="Model", readonly=True)
    arguments = fields.Text(readonly=True)
    preview = fields.Text(
        readonly=True,
        help="What the call would do, produced by running the tool in dry-run mode when "
             "it supports one. Nothing was written to generate this.")
    state = fields.Selection(
        [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"),
         ("expired", "Expired")],
        default="pending", required=True, readonly=True, index=True)
    decided_by_id = fields.Many2one("res.users", string="Decided By", readonly=True)
    decided_at = fields.Datetime(readonly=True)
    decision_note = fields.Char()
    result_summary = fields.Char(readonly=True)

    def action_approve(self):
        """Approve and run the call, as the user who requested it."""
        for approval in self:
            if approval.state != "pending":
                raise UserError(_("This request has already been decided."))
            args = json.loads(approval.arguments or "{}")
            # Runs as the user the agent authenticated as, not as the approver:
            # approving means "yes, let that account do this", not "do it with my
            # rights". The tool record itself stays in sudo as configuration.
            env = self.env(user=approval.requested_by_id.id)
            tool = approval.tool_id.sudo()
            try:
                text, _structured, touched = tool.execute(env, args)
                approval.write({
                    "state": "approved", "decided_by_id": self.env.uid,
                    "decided_at": fields.Datetime.now(), "result_summary": text,
                })
                self.env["mdx.mcp.call"]._record(
                    tool_id=tool.id, tool_name=tool.name, user_id=approval.requested_by_id.id,
                    client_name=approval.client_name, model_name=approval.model_name,
                    arguments=args, status="ok", result_summary=text,
                    records_touched=touched, is_write=True, approval_id=approval.id)
            except Exception as error:  # noqa: BLE001
                approval.write({
                    "state": "approved", "decided_by_id": self.env.uid,
                    "decided_at": fields.Datetime.now(),
                    "result_summary": _("Approved but failed: %s", error),
                })
                raise
        return True

    def action_reject(self):
        for approval in self:
            if approval.state != "pending":
                raise UserError(_("This request has already been decided."))
        return self.write({
            "state": "rejected", "decided_by_id": self.env.uid,
            "decided_at": fields.Datetime.now(),
        })
