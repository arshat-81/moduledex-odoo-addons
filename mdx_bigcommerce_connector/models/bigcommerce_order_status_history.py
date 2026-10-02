from odoo import fields, models


class BigcommerceOrderStatusHistory(models.Model):
    _name = "bigcommerce.order.status.history"
    _description = "BigCommerce Order Status History"
    _order = "changed_at desc"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    previous_status = fields.Char()
    status = fields.Char(required=True)
    changed_at = fields.Datetime(default=fields.Datetime.now, required=True)
    source = fields.Selection(
        [("sync", "Manual/Cron Sync"), ("webhook", "Webhook")], required=True, default="sync",
    )
