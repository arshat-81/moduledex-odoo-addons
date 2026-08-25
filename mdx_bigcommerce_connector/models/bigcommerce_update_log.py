from odoo import fields, models


class BigcommerceUpdateLog(models.Model):
    _name = "bigcommerce.update.log"
    _description = "BigCommerce Sync Log"
    _order = "create_date desc"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    operation = fields.Char(required=True, help="e.g. sync_products, sync_orders, webhook:store/order/created")
    status = fields.Selection(
        [("success", "Success"), ("warning", "Warning"), ("error", "Error")],
        default="success", required=True,
    )
    message = fields.Text()
    record_count = fields.Integer()

    # Per-record change tracking, used by bulk push (Odoo -> BigCommerce) so
    # a run's outcome is auditable line by line, not just one aggregate blob.
    run_id = fields.Char(index=True, help="Groups every log line from one wizard run together.")
    field_updated = fields.Char(
        help="Which attribute was pushed, e.g. price, quantity, publish.",
    )
    variant_id = fields.Many2one("bigcommerce.product.variant", ondelete="set null")
    old_value = fields.Char()
    new_value = fields.Char()
    user_id = fields.Many2one("res.users", default=lambda self: self.env.uid, readonly=True)

    @classmethod
    def log(cls, config, operation, status="success", message="", record_count=0):
        return config.env["bigcommerce.update.log"].sudo().create({
            "config_id": config.id,
            "operation": operation,
            "status": status,
            "message": message,
            "record_count": record_count,
        })

    @classmethod
    def log_change(cls, config, run_id, field_updated, variant, old_value, new_value, status, message=""):
        return config.env["bigcommerce.update.log"].sudo().create({
            "config_id": config.id,
            "operation": "bulk_push_to_bigcommerce",
            "run_id": run_id,
            "field_updated": field_updated,
            "variant_id": variant.id,
            "old_value": "" if old_value is None else str(old_value),
            "new_value": "" if new_value is None else str(new_value),
            "status": status,
            "message": message,
        })
