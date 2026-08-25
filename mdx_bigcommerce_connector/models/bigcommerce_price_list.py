from odoo import fields, models


class BigcommercePriceList(models.Model):
    _name = "bigcommerce.price.list"
    _description = "BigCommerce Price List"
    _rec_name = "name"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    bc_price_list_id = fields.Char(string="BigCommerce Price List ID", required=True)
    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Odoo Pricelist",
        help="Records synced from this BigCommerce Price List become items on this Odoo pricelist.",
    )
    channel_ids = fields.Many2many("bigcommerce.channel", string="Assigned Storefronts")
    customer_group_ids = fields.Many2many("bigcommerce.customer.group", string="Assigned Customer Groups")
    record_count = fields.Integer(compute="_compute_record_count")

    _bc_price_list_uniq = models.Constraint(
        "unique(config_id, bc_price_list_id)",
        "A BigCommerce price list can only be mirrored once per store.",
    )

    def _compute_record_count(self):
        for price_list in self:
            price_list.record_count = self.env["bigcommerce.price.list.record"].search_count(
                [("price_list_id", "=", price_list.id)])

    @classmethod
    def sync_from_bigcommerce(cls, config):
        env = config.env
        # active_test=False: same reasoning as bigcommerce.channel — an
        # inactive price list must still be findable on re-sync, or Odoo's
        # default search() hides it and every retry tries to recreate it.
        PriceList = env["bigcommerce.price.list"].with_context(active_test=False)
        price_lists = config._request_all_pages("pricelists")
        synced = PriceList.browse()
        for data in price_lists:
            bc_id = str(data.get("id"))
            existing = PriceList.search([
                ("config_id", "=", config.id), ("bc_price_list_id", "=", bc_id),
            ], limit=1)
            vals = {
                "config_id": config.id,
                "bc_price_list_id": bc_id,
                "name": data.get("name") or bc_id,
                "active": bool(data.get("active", True)),
            }
            if existing:
                existing.write(vals)
            else:
                existing = PriceList.create(vals)
            synced |= existing
            existing.sync_records()
        return synced

    def sync_records(self):
        self.ensure_one()
        Record = self.env["bigcommerce.price.list.record"]
        Variant = self.env["bigcommerce.product.variant"]
        rows = self.config_id._request_all_pages(f"pricelists/{self.bc_price_list_id}/records")
        for row in rows:
            variant_bc_id = str(row.get("variant_id"))
            existing = Record.search([
                ("price_list_id", "=", self.id), ("bc_variant_id", "=", variant_bc_id),
            ], limit=1)
            variant = Variant.search([
                ("config_id", "=", self.config_id.id), ("bc_variant_id", "=", variant_bc_id),
            ], limit=1)
            vals = {
                "price_list_id": self.id,
                "bc_variant_id": variant_bc_id,
                "variant_id": variant.id if variant else False,
                "price": row.get("price") or 0.0,
                "sale_price": row.get("sale_price") or 0.0,
            }
            if existing:
                existing.write(vals)
            else:
                existing = Record.create(vals)
            existing._apply_to_pricelist()


class BigcommercePriceListRecord(models.Model):
    _name = "bigcommerce.price.list.record"
    _description = "BigCommerce Price List Record"

    price_list_id = fields.Many2one("bigcommerce.price.list", required=True, ondelete="cascade")
    bc_variant_id = fields.Char(string="BigCommerce Variant ID", required=True)
    variant_id = fields.Many2one("bigcommerce.product.variant", string="Variant")
    product_id = fields.Many2one(related="variant_id.product_id", store=True)
    price = fields.Float()
    sale_price = fields.Float()

    _bc_pricelist_record_uniq = models.Constraint(
        "unique(price_list_id, bc_variant_id)",
        "A variant can only appear once per BigCommerce price list.",
    )

    def _apply_to_pricelist(self):
        """Upsert a fixed-price product.pricelist.item on the linked Odoo
        pricelist for this variant, so Odoo-side quoting matches BigCommerce."""
        self.ensure_one()
        pricelist = self.price_list_id.pricelist_id
        if not pricelist or not self.product_id:
            return
        amount = self.sale_price or self.price
        if not amount:
            return
        Item = self.env["product.pricelist.item"]
        item = Item.search([
            ("pricelist_id", "=", pricelist.id),
            ("product_id", "=", self.product_id.id),
            ("applied_on", "=", "0_product_variant"),
        ], limit=1)
        vals = {
            "pricelist_id": pricelist.id,
            "applied_on": "0_product_variant",
            "product_id": self.product_id.id,
            "compute_price": "fixed",
            "fixed_price": amount,
        }
        if item:
            item.write(vals)
        else:
            Item.create(vals)
