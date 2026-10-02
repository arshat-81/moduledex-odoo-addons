from odoo import fields, models


class BigcommerceChannel(models.Model):
    _name = "bigcommerce.channel"
    _description = "BigCommerce Storefront / Channel"
    _rec_name = "name"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    bc_channel_id = fields.Char(string="BigCommerce Channel ID", required=True)
    name = fields.Char(required=True)
    platform = fields.Char(help="e.g. bigcommerce, catalyst, custom — as reported by the API")
    channel_type = fields.Char(help="storefront, marketplace, etc.")
    is_default = fields.Boolean()
    active = fields.Boolean(default=True)
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Odoo Pricelist",
        help="Orders and price-list sync for this storefront resolve against this pricelist.",
    )

    _bc_channel_uniq = models.Constraint(
        "unique(config_id, bc_channel_id)",
        "A BigCommerce channel can only be mirrored once per store.",
    )

    @classmethod
    def sync_from_bigcommerce(cls, config):
        env = config.env
        # active_test=False: a channel BigCommerce reports as e.g. "prelaunch"
        # (any real dev/trial store) maps to active=False here, and Odoo's
        # default search() silently hides inactive records — without this,
        # every re-sync would fail to find it and try to recreate it,
        # hitting the unique constraint below.
        Channel = env["bigcommerce.channel"].with_context(active_test=False)
        channels = config._request_all_pages("channels")
        synced = Channel.browse()
        for data in channels:
            bc_id = str(data.get("id"))
            existing = Channel.search([
                ("config_id", "=", config.id), ("bc_channel_id", "=", bc_id),
            ], limit=1)
            vals = {
                "config_id": config.id,
                "bc_channel_id": bc_id,
                "name": data.get("name") or bc_id,
                "platform": data.get("platform"),
                "channel_type": data.get("type"),
                "is_default": bool(data.get("is_listable_from_ui")) and data.get("id") == 1,
                # BigCommerce's "status" is a richer state (prelaunch, active,
                # disconnected...), not a boolean — is_enabled is what
                # actually reflects whether the channel is usable.
                "active": bool(data.get("is_enabled", True)),
            }
            if existing:
                existing.write(vals)
            else:
                existing = Channel.create(vals)
            synced |= existing
        return synced
