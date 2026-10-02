from datetime import timedelta

from odoo import api, fields, models


class BigcommerceDashboard(models.TransientModel):
    _name = "bigcommerce.dashboard"
    _description = "BigCommerce Dashboard"

    @api.model
    def get_dashboard_data(self, config_id=None):
        today = fields.Date.today()
        week_start = today - timedelta(days=7)
        month_start = today - timedelta(days=30)

        Order = self.env["sale.order"]
        base = [("bigcommerce_config_id", "=", int(config_id))] if config_id else \
            [("bigcommerce_order_id", "!=", False)]

        def window(date_from):
            domain = base + [("bigcommerce_created_at", ">=", str(date_from) + " 00:00:00")]
            orders = Order.search(domain)
            return {
                "orders": len(orders),
                "revenue": round(sum(orders.mapped("amount_total")), 2),
            }

        channels = self.env["bigcommerce.channel"].search(
            [("config_id", "=", int(config_id))] if config_id else [])
        per_channel = []
        for channel in channels:
            orders = Order.search(base + [("bigcommerce_channel_id", "=", channel.id)])
            per_channel.append({
                "name": channel.name,
                "orders": len(orders),
                "revenue": round(sum(orders.mapped("amount_total")), 2),
            })

        Cart = self.env["bigcommerce.cart"]
        cart_domain = [("config_id", "=", int(config_id))] if config_id else []
        abandoned = Cart.search(cart_domain + [("state", "=", "abandoned")])
        converted = Cart.search(cart_domain + [("state", "=", "converted")])

        return {
            "today": window(today),
            "last_7_days": window(week_start),
            "last_30_days": window(month_start),
            "all_time": window(fields.Date.from_string("1970-01-01")),
            "per_channel": per_channel,
            "carts_abandoned": len(abandoned),
            "carts_converted": len(converted),
            "carts_recovery_rate": round(
                (len(converted) / (len(abandoned) + len(converted)) * 100), 1,
            ) if (abandoned or converted) else 0.0,
            "product_count": self.env["bigcommerce.product"].search_count(
                [("config_id", "=", int(config_id))] if config_id else []),
        }
