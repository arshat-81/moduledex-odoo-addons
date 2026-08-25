import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

WEBHOOK_SCOPES = [
    "store/order/created",
    "store/order/updated",
    "store/order/statusUpdated",
    "store/order/refund/created",
    "store/product/created",
    "store/product/updated",
    "store/product/inventory/updated",
    "store/cart/updated",
    "store/cart/abandoned",
    "store/cart/converted",
]


class BigcommerceWebhook(models.Model):
    _name = "bigcommerce.webhook"
    _description = "BigCommerce Registered Webhook"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    scope = fields.Char(required=True)
    bc_webhook_id = fields.Char(string="BigCommerce Webhook ID")
    destination = fields.Char()
    active = fields.Boolean(default=True)
    is_disabled_by_bc = fields.Boolean(
        string="Auto-disabled by BigCommerce",
        help="BigCommerce disables a webhook automatically after exhausting "
             "delivery retries. Flagged here so it doesn't fail silently.",
    )
    last_delivery_exception = fields.Char()

    @classmethod
    def register_all(cls, config):
        env = config.env
        Webhook = env["bigcommerce.webhook"]
        destination_base = config.webhook_base_url
        registered = Webhook.browse()
        for scope in WEBHOOK_SCOPES:
            existing = Webhook.search([
                ("config_id", "=", config.id), ("scope", "=", scope),
            ], limit=1)
            payload = {
                "scope": scope,
                "destination": destination_base,
                "is_active": True,
            }
            try:
                data = config._request("POST", "hooks", version="v3", json_body=payload)
                bc_id = str((data.get("data") or {}).get("id") or "")
            except Exception:
                _logger.exception("Failed to register BigCommerce webhook for scope %s", scope)
                continue
            vals = {
                "config_id": config.id, "scope": scope,
                "bc_webhook_id": bc_id, "destination": destination_base, "active": True,
            }
            if existing:
                existing.write(vals)
            else:
                existing = Webhook.create(vals)
            registered |= existing
        return registered

    def mark_delivery_exception(self, exception_type, message=""):
        self.ensure_one()
        vals = {"last_delivery_exception": f"{exception_type}: {message}"[:250]}
        if exception_type == "attempts_exhausted":
            vals["is_disabled_by_bc"] = True
        self.write(vals)
