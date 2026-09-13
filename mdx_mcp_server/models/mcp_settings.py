"""Server-wide settings, and the key a client authenticates with."""

from odoo import _, api, fields, models

# res.users.apikeys is scoped: a key minted here cannot be used for general RPC,
# and an RPC key cannot be used here.
MCP_SCOPE = "mdx_mcp_server"


class ResUsers(models.Model):
    _inherit = "res.users"

    def action_mcp_generate_key(self):
        """Mint an MCP key for this user and show it once."""
        self.ensure_one()
        # with_user(self) makes the key belong to this user - _generate reads
        # env.user for ownership - and sudo() is what permits a key with no
        # expiry, which Odoo otherwise reserves for system users. The button
        # itself is restricted to MCP administrators, so this grants nothing a
        # caller did not already have.
        key = self.env["res.users.apikeys"].with_user(self).sudo()._generate(
            MCP_SCOPE, _("MCP client"), False)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("MCP key for %s", self.name),
                "message": _("Copy it now, it is not shown again: %s\n\nThis key does not expire. Revoke it from the user's Account Security page.", key),
                "sticky": True, "type": "warning",
            },
        }


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    mcp_masked_fields = fields.Char(
        string="Additional Masked Fields",
        config_parameter="mdx_mcp_server.masked_fields",
        help="Comma separated model.field entries whose values are replaced before any "
             "result leaves the server, on top of the built-in credential fields.")
    mcp_require_approval_all_writes = fields.Boolean(
        string="Approve Every Write",
        config_parameter="mdx_mcp_server.require_approval_all_writes",
        help="Park every create, update and delete for a human, whatever the individual "
             "tool says. The safest way to start.")
