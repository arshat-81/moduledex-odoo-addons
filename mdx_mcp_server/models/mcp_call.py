"""The record of what an agent actually did."""

import json
import logging

import psycopg2

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class MdxMcpCall(models.Model):
    _name = "mdx.mcp.call"
    _description = "MCP Call"
    _order = "create_date desc, id desc"
    _rec_name = "tool_name"

    tool_id = fields.Many2one("mdx.mcp.tool", string="Tool", ondelete="set null", index=True)
    tool_name = fields.Char(readonly=True, index=True,
                            help="Kept as text so the log survives the tool being deleted.")
    user_id = fields.Many2one("res.users", string="Called As", readonly=True, index=True)
    api_key_name = fields.Char(string="Key", readonly=True)
    client_name = fields.Char(readonly=True, help="Whatever the client called itself.")
    model_name = fields.Char(string="Model", readonly=True, index=True)
    arguments = fields.Text(readonly=True)
    status = fields.Selection(
        [("ok", "Succeeded"), ("error", "Failed"), ("denied", "Denied"),
         ("pending", "Awaiting Approval")],
        required=True, readonly=True, index=True)
    result_summary = fields.Char(readonly=True)
    records_touched = fields.Integer(readonly=True)
    duration_ms = fields.Integer(readonly=True)
    is_write = fields.Boolean(readonly=True, index=True)
    approval_id = fields.Many2one("mdx.mcp.approval", readonly=True, ondelete="set null")

    @api.model
    def _record(self, **values):
        """Write one log row.

        A bad value must not fail the tool call, so application-level problems
        are swallowed. A *database* error is not swallowed: it leaves the cursor
        aborted, so hiding it would only turn one clear failure into a confusing
        cascade, and Odoo's own retry - including the retry that upgrades a
        read-only cursor to read/write - can only fire if it propagates.
        """
        try:
            if isinstance(values.get("arguments"), (dict, list)):
                values["arguments"] = json.dumps(values["arguments"], indent=2,
                                                 sort_keys=True, default=str)[:20000]
            return self.sudo().create(values)
        except psycopg2.Error:
            raise
        except Exception:  # noqa: BLE001
            _logger.exception("mdx_mcp_server: could not write the call log")
            return self.sudo().browse()
