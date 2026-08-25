import logging
from email.utils import parsedate_to_datetime

from odoo import _, fields, models

_logger = logging.getLogger(__name__)


def _parse_bc_datetime(value):
    """BigCommerce's v2 API returns dates in RFC 2822 format
    ("Tue, 18 Aug 2026 11:41:28 +0000"), not the format Odoo's Datetime
    field expects — parse and drop the tzinfo (Odoo stores naive UTC)."""
    if not value:
        return False
    try:
        return parsedate_to_datetime(value).replace(tzinfo=None)
    except (TypeError, ValueError):
        return False


class SaleOrder(models.Model):
    _inherit = "sale.order"

    bigcommerce_config_id = fields.Many2one("bigcommerce.config", readonly=True)
    bigcommerce_channel_id = fields.Many2one("bigcommerce.channel", readonly=True)
    bigcommerce_order_id = fields.Char(readonly=True, index=True)
    bigcommerce_status = fields.Char(readonly=True)
    bigcommerce_created_at = fields.Datetime(readonly=True)
    refund_ids = fields.One2many("bigcommerce.refund", "order_id")
    bigcommerce_status_history_ids = fields.One2many(
        "bigcommerce.order.status.history", "order_id", string="Status History",
    )

    _bigcommerce_order_uniq = models.Constraint(
        "unique(bigcommerce_config_id, bigcommerce_order_id)",
        "A BigCommerce order can only be imported once.",
    )

    @classmethod
    def bigcommerce_sync_orders(cls, config):
        env = config.env
        Order = env["sale.order"]
        # BigCommerce's v2 orders endpoint doesn't support a "not equal"/"not
        # in" filter operator on status_id (confirmed against the live API —
        # it 400s), so cancelled orders are filtered client-side instead.
        rows = config._request_all_pages("orders", version="v2")
        if not config.import_cancelled:
            rows = [row for row in rows if row.get("status_id") != 5]
        synced = Order.browse()
        for data in rows:
            synced |= Order._bigcommerce_upsert(config, data)
        return synced

    @classmethod
    def _bigcommerce_upsert(cls, config, data, source="sync"):
        env = config.env
        Order = env["sale.order"]
        Partner = env["res.partner"]
        Channel = env["bigcommerce.channel"]
        Group = env["bigcommerce.customer.group"]

        bc_id = str(data.get("id"))
        existing = Order.search([
            ("bigcommerce_config_id", "=", config.id), ("bigcommerce_order_id", "=", bc_id),
        ], limit=1)

        billing = data.get("billing_address") or {}
        email = billing.get("email") or data.get("billing_address", {}).get("email")
        partner = Partner.browse()
        if email:
            partner = Partner.search([("email", "=", email)], limit=1)
        if not partner and (billing.get("first_name") or billing.get("last_name")):
            partner = Partner.create({
                "name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip() or email,
                "email": email,
                "street": billing.get("street_1"),
                "city": billing.get("city"),
                "zip": billing.get("zip"),
                "phone": billing.get("phone"),
            })

        group = Group.search([
            ("config_id", "=", config.id), ("bc_group_id", "=", str(data.get("customer_group_id") or "0")),
        ], limit=1)
        if partner and group.pricelist_id:
            partner.property_product_pricelist = group.pricelist_id

        channel = Channel.search([
            ("config_id", "=", config.id), ("bc_channel_id", "=", str(data.get("channel_id") or "1")),
        ], limit=1)

        # Fields safe to overwrite on every re-sync, confirmed or not.
        vals = {
            "bigcommerce_config_id": config.id,
            "bigcommerce_channel_id": channel.id if channel else False,
            "bigcommerce_order_id": bc_id,
            "bigcommerce_status": data.get("status"),
            "bigcommerce_created_at": _parse_bc_datetime(data.get("date_created")),
        }
        new_status = data.get("status")
        if existing:
            # partner_id/pricelist_id are set once at creation only — Odoo
            # outright blocks changing pricelist_id on a confirmed order
            # ("You cannot change the pricelist of a confirmed order!"), and
            # a re-sync has no business reassigning either on an order that
            # already exists.
            previous_status = existing.bigcommerce_status
            existing.write(vals)
            order = existing
        else:
            previous_status = False
            vals["partner_id"] = partner.id if partner else env.user.partner_id.id
            vals["pricelist_id"] = (
                group.pricelist_id.id if group and group.pricelist_id else config.pricelist_id.id
            ) or None
            order = Order.create(vals)
            order._bigcommerce_sync_lines(config, bc_id)

        if new_status and new_status != previous_status:
            env["bigcommerce.order.status.history"].sudo().create({
                "order_id": order.id,
                "previous_status": previous_status,
                "status": new_status,
                "source": source,
            })

        order._bigcommerce_check_totals(config, data)
        order._bigcommerce_apply_workflow(config, data)
        return order

    def _bigcommerce_check_totals(self, config, data):
        """Warn if Odoo's computed tax does not match what BigCommerce charged.

        Mapping a derived rate back onto an Odoo tax can drift - rounding per
        line, a rate that resolves to the wrong record, or tax on shipping that
        Odoo has no line for. Silent drift is the dangerous case: the order
        looks fine and the VAT return is wrong. Flag it instead of hiding it.
        """
        self.ensure_one()
        if config.tax_mode != "map":
            return
        try:
            bc_tax = float(data.get("total_tax") or 0.0)
            bc_total = float(data.get("total_inc_tax") or 0.0)
        except (TypeError, ValueError):
            return

        tax_gap = abs(self.amount_tax - bc_tax)
        total_gap = abs(self.amount_total - bc_total)
        if tax_gap <= 0.01 and total_gap <= 0.01:
            return

        self.env["bigcommerce.update.log"].log(
            config, "order_tax_mismatch", status="error",
            message=_(
                "Order %(ref)s: Odoo computed tax %(odoo_tax)s vs BigCommerce %(bc_tax)s "
                "(total %(odoo_total)s vs %(bc_total)s). Shipping tax and per-line rounding "
                "are the usual causes; check the tax mapping for this store.",
                ref=self.name, odoo_tax=self.amount_tax, bc_tax=bc_tax,
                odoo_total=self.amount_total, bc_total=bc_total))

    def _bigcommerce_sync_lines(self, config, bc_order_id):
        self.ensure_one()
        Variant = self.env["bigcommerce.product.variant"]
        TaxMap = self.env["bigcommerce.tax.mapping"]
        products = config._request_all_pages(f"orders/{bc_order_id}/products", version="v2")
        order_lines = []
        untaxed = []
        for row in products:
            variant = Variant.search([
                ("config_id", "=", config.id), ("bc_variant_id", "=", str(row.get("variant_id"))),
            ], limit=1)
            if not variant or not variant.product_id:
                continue
            # Never inherit the product's own default taxes: the price was already
            # finalised on BigCommerce, so Odoo re-taxing it makes the total diverge
            # from what the customer actually paid. Taxes are set explicitly from
            # what BigCommerce reports on the line, or not at all.
            tax_ids = []
            if config.tax_mode == "map":
                rate = TaxMap.line_rate(row)
                if rate:
                    tax = TaxMap.resolve(config, rate, row.get("tax_class_id"))
                    if tax:
                        tax_ids = tax.ids
                    else:
                        untaxed.append((row.get("name") or row.get("sku") or "?", rate))

            order_lines.append((0, 0, {
                "product_id": variant.product_id.id,
                "product_uom_qty": row.get("quantity") or 1,
                "price_unit": row.get("price_ex_tax") or row.get("base_price") or 0.0,
                "tax_ids": [(6, 0, tax_ids)],
            }))
        if order_lines:
            self.write({"order_line": order_lines})
        if untaxed:
            self.env["bigcommerce.update.log"].log(
                config, "order_tax",
                status="error" if config.tax_mode == "map" else "success",
                message=_(
                    "Order %(ref)s: no Odoo tax for %(count)s line(s) - %(detail)s. "
                    "Map the rate under Configuration > Tax Mappings, or enable "
                    "'Create missing taxes'.",
                    ref=self.name, count=len(untaxed),
                    detail=", ".join("%s @ %.4g%%" % (n, r) for n, r in untaxed[:5])))

    def _bigcommerce_apply_workflow(self, config, data):
        self.ensure_one()
        workflow = config.auto_workflow
        if workflow == "quotation":
            return
        if self.state == "draft":
            self.action_confirm()
        if workflow in ("invoice", "paid") and not self.invoice_ids:
            invoice = self._create_invoices()
            invoice.action_post()
            if workflow == "paid":
                payment_status = (data.get("payment_status") or "").lower()
                if payment_status in ("captured", "paid"):
                    self.env["account.payment.register"].with_context(
                        active_model="account.move", active_ids=invoice.ids,
                    ).create({}).action_create_payments()

    def action_bigcommerce_resync(self):
        """Re-pull just this order from BigCommerce, on demand."""
        for order in self:
            if not order.bigcommerce_order_id:
                continue
            data = order.bigcommerce_config_id._request(
                "GET", f"orders/{order.bigcommerce_order_id}", version="v2")
            if data:
                self._bigcommerce_upsert(order.bigcommerce_config_id, data)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": "BigCommerce",
                       "message": _("Re-synced %(count)s order(s).", count=len(self)),
                       "type": "success"},
        }

    def _bigcommerce_handle_refund(self, config, payload):
        self.ensure_one()
        self.env["bigcommerce.refund"].create_from_webhook(config, self, payload)
