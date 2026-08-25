import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BigcommercePublishWizard(models.TransientModel):
    """Create Odoo products on BigCommerce as brand-new catalog products.

    This is the opposite direction from the bulk update wizard: that one
    updates price/stock on products that already exist on BigCommerce, this
    one creates products there that don't exist yet.
    """

    _name = "bigcommerce.publish.wizard"
    _description = "Publish Odoo products to BigCommerce"

    config_id = fields.Many2one("bigcommerce.config", required=True,
                                default=lambda self: self.env["bigcommerce.config"].search(
                                    [("state", "=", "connected")], limit=1))
    product_tmpl_ids = fields.Many2many("product.template", string="Products", required=True)
    category_id = fields.Many2one(
        "bigcommerce.category", string="BigCommerce Category",
        domain="[('config_id', '=', config_id)]",
        help="Optional. Products are created in this storefront category.",
    )
    is_visible = fields.Boolean(string="Visible on storefront", default=True)
    push_inventory = fields.Boolean(string="Push current stock", default=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        if ctx.get("active_model") == "product.template":
            res["product_tmpl_ids"] = [(6, 0, ctx.get("active_ids", []))]
        elif ctx.get("active_model") == "product.product":
            products = self.env["product.product"].browse(ctx.get("active_ids", []))
            res["product_tmpl_ids"] = [(6, 0, products.product_tmpl_id.ids)]
        return res

    def _stock_for(self, product):
        return int(sum(self.env["stock.quant"].sudo().search([
            ("product_id", "=", product.id),
            ("location_id", "=", self.config_id.warehouse_id.lot_stock_id.id),
        ]).mapped("quantity")))

    def action_publish(self):
        self.ensure_one()
        config = self.config_id
        BcProduct = self.env["bigcommerce.product"]
        Log = self.env["bigcommerce.update.log"]

        created, skipped, failed = 0, 0, []

        for tmpl in self.product_tmpl_ids:
            product = tmpl.product_variant_id
            sku = tmpl.default_code or f"ODOO-{tmpl.id}"

            # Don't create a duplicate if this SKU is already mirrored.
            already = self.env["bigcommerce.product.variant"].search([
                ("config_id", "=", config.id), ("sku", "=", sku),
            ], limit=1)
            if already:
                skipped += 1
                continue

            body = {
                "name": tmpl.name,
                "type": "physical" if tmpl.type == "consu" else "digital",
                "price": tmpl.list_price,
                "weight": tmpl.weight or 0,
                "sku": sku,
                "is_visible": self.is_visible,
            }
            if tmpl.description_sale:
                body["description"] = tmpl.description_sale
            if self.category_id:
                body["categories"] = [int(self.category_id.bc_category_id)]
            if self.push_inventory:
                body["inventory_level"] = self._stock_for(product)
                body["inventory_tracking"] = "product"

            try:
                response = config._request("POST", "catalog/products", version="v3", json_body=body)
            except Exception as exc:  # noqa: BLE001 — one failure shouldn't stop the batch
                failed.append(f"{tmpl.name}: {exc}")
                Log.log(config, "publish_to_bigcommerce", status="error",
                        message=f"{tmpl.name} ({sku}): {exc}")
                continue

            data = response.get("data") or {}
            if not data.get("id"):
                failed.append(f"{tmpl.name}: unexpected response")
                continue

            # Mirror it locally straight away, including the variant link back
            # to this Odoo product, so later price/stock pushes and pulls know
            # which BigCommerce record this maps to.
            bc_product = BcProduct._sync_one(config, data)
            bc_product.variant_ids.filtered(lambda v: not v.product_id).write({"product_id": product.id})
            created += 1

        if not created and failed:
            raise UserError(_("Nothing was published:\n%(errors)s", errors="\n".join(failed)))

        Log.log(config, "publish_to_bigcommerce", record_count=created,
                status="warning" if failed else "success",
                message="\n".join(failed) if failed else "")

        message = _("Published %(count)s product(s) to BigCommerce.", count=created)
        if skipped:
            message += "\n" + _("%(count)s skipped (already on BigCommerce).", count=skipped)
        if failed:
            message += "\n" + _("%(count)s failed — see the sync log.", count=len(failed))
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("BigCommerce"), "message": message,
                       "type": "warning" if failed else "success", "sticky": bool(failed)},
        }
