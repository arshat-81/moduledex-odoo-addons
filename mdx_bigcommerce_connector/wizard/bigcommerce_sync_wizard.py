from odoo import fields, models


class BigcommerceSyncWizard(models.TransientModel):
    _name = "bigcommerce.sync.wizard"
    _description = "BigCommerce Manual Sync"

    config_id = fields.Many2one("bigcommerce.config", required=True)
    sync_channels = fields.Boolean(default=True)
    sync_categories = fields.Boolean(default=True)
    sync_customer_groups = fields.Boolean(default=True)
    sync_price_lists = fields.Boolean(default=True)
    sync_customers = fields.Boolean(default=True)
    sync_products = fields.Boolean(default=True)
    sync_orders = fields.Boolean(default=True)

    def action_sync_now(self):
        self.ensure_one()
        config = self.config_id
        if self.sync_channels:
            config.action_sync_channels()
        if self.sync_categories:
            config.action_sync_categories()
        if self.sync_customer_groups:
            config.action_sync_customer_groups()
        if self.sync_price_lists:
            config.action_sync_price_lists()
        if self.sync_customers:
            config.action_sync_customers()
        if self.sync_products:
            config.action_sync_products()
        if self.sync_orders:
            config.action_sync_orders()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "BigCommerce", "message": "Sync complete.", "type": "success"},
        }
