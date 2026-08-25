import base64
import logging

import requests

from odoo import _, fields, models

_logger = logging.getLogger(__name__)


class BigcommerceProduct(models.Model):
    """Mirrors one BigCommerce catalog product. Deliberately does NOT try to
    reproduce Odoo's attribute-line variant system — each BigCommerce variant
    (BigCommerce products always have at least one) becomes its own
    product.product, grouped back to this record. Simpler and more reliable
    than faking attribute-driven variant generation blind."""

    _name = "bigcommerce.product"
    _description = "BigCommerce Product"
    _rec_name = "name"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    bc_product_id = fields.Char(string="BigCommerce Product ID", required=True)
    name = fields.Char(required=True)
    sku = fields.Char()
    brand_name = fields.Char()
    category_ids = fields.Many2many("bigcommerce.category", string="Categories")
    is_visible = fields.Boolean(default=True)
    inventory_tracking = fields.Selection(
        [("none", "None"), ("product", "By Product"), ("variant", "By Variant")],
        default="product",
    )
    variant_ids = fields.One2many("bigcommerce.product.variant", "bigcommerce_product_id", string="Variants")
    modifier_ids = fields.One2many("bigcommerce.product.modifier", "bigcommerce_product_id", string="Modifiers")
    image_ids = fields.One2many("bigcommerce.product.image", "bigcommerce_product_id", string="Images")
    variant_count = fields.Integer(compute="_compute_variant_count")
    last_synced = fields.Datetime(readonly=True)
    sync_state = fields.Selection(
        [("pending", "Pending"), ("synced", "Synced"), ("error", "Error")],
        default="pending", required=True,
    )
    sync_error = fields.Text()

    _bc_product_uniq = models.Constraint(
        "unique(config_id, bc_product_id)",
        "A BigCommerce product can only be mirrored once per store.",
    )

    def _compute_variant_count(self):
        for product in self:
            product.variant_count = len(product.variant_ids)

    def action_resync(self):
        """Re-pull just this product from BigCommerce, on demand."""
        for product in self:
            data = product.config_id._request(
                "GET", f"catalog/products/{product.bc_product_id}", version="v3",
                params={"include": "variants,images,modifiers,custom_fields"},
            ).get("data")
            if data:
                self._sync_one(product.config_id, data)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": "BigCommerce",
                       "message": _("Re-synced %(count)s product(s).", count=len(self)),
                       "type": "success"},
        }

    @classmethod
    def sync_from_bigcommerce(cls, config):
        env = config.env
        Product = env["bigcommerce.product"]
        rows = config._request_all_pages(
            "catalog/products",
            params={"include": "variants,images,modifiers,custom_fields"},
        )
        synced = Product.browse()
        for data in rows:
            try:
                # A savepoint here matters, not just the try/except: without
                # it, a real Postgres-level error (e.g. a constraint
                # violation) poisons the whole shared transaction and every
                # subsequent product in this loop fails too, not just the
                # one that actually broke.
                with env.cr.savepoint():
                    synced |= Product._sync_one(config, data)
            except Exception as exc:  # noqa: BLE001 — a product that can't even be created shouldn't stop the rest
                _logger.exception("BigCommerce product sync failed for id=%s before creation", data.get("id"))
                env["bigcommerce.update.log"].log(
                    config, "sync_products", status="error",
                    message=f"Product {data.get('id')} ({data.get('name')}): {exc}",
                )
        return synced

    @classmethod
    def _sync_one(cls, config, data):
        env = config.env
        Product = env["bigcommerce.product"]
        Category = env["bigcommerce.category"]
        bc_id = str(data.get("id"))
        existing = Product.search([
            ("config_id", "=", config.id), ("bc_product_id", "=", bc_id),
        ], limit=1)

        category_ids = Category.search([
            ("config_id", "=", config.id),
            ("bc_category_id", "in", [str(c) for c in (data.get("categories") or [])]),
        ]).ids

        vals = {
            "config_id": config.id,
            "bc_product_id": bc_id,
            "name": data.get("name") or bc_id,
            "sku": data.get("sku"),
            "brand_name": (data.get("brand") or {}).get("name") if isinstance(data.get("brand"), dict) else False,
            "is_visible": bool(data.get("is_visible", True)),
            "inventory_tracking": data.get("inventory_tracking") or "product",
            "category_ids": [(6, 0, category_ids)],
            "last_synced": fields.Datetime.now(),
        }
        if existing:
            existing.write(vals)
        else:
            existing = Product.create(vals)

        try:
            with env.cr.savepoint():
                existing._sync_variants(data.get("variants") or [], data)
                existing._sync_modifiers(data.get("modifiers") or [])
                existing._sync_images(data.get("images") or [])
        except Exception as exc:  # noqa: BLE001 — one bad product shouldn't abort the whole catalog sync
            _logger.exception("BigCommerce product sync failed for bc_product_id=%s", bc_id)
            existing.write({"sync_state": "error", "sync_error": str(exc)})
            env["bigcommerce.update.log"].log(
                config, "sync_products", status="error",
                message=f"Product {bc_id} ({existing.name}): {exc}",
            )
        else:
            existing.write({"sync_state": "synced", "sync_error": False})
        return existing

    def _sync_variants(self, variant_rows, product_data):
        self.ensure_one()
        Variant = self.env["bigcommerce.product.variant"]
        if not variant_rows:
            # BigCommerce still models a "simple" product as a single implicit
            # variant sharing the product's own id/sku/price.
            variant_rows = [{
                "id": self.bc_product_id, "sku": self.sku,
                "price": product_data.get("price"), "sale_price": product_data.get("sale_price"),
                "inventory_level": product_data.get("inventory_level"),
                "weight": product_data.get("weight"),
                "option_values": [],
            }]
        for row in variant_rows:
            Variant._sync_one(self, row)

    def _sync_modifiers(self, modifier_rows):
        self.ensure_one()
        Modifier = self.env["bigcommerce.product.modifier"]
        for row in modifier_rows:
            bc_id = str(row.get("id"))
            existing = Modifier.search([
                ("bigcommerce_product_id", "=", self.id), ("bc_modifier_id", "=", bc_id),
            ], limit=1)
            vals = {
                "bigcommerce_product_id": self.id,
                "bc_modifier_id": bc_id,
                "name": row.get("display_name") or row.get("name") or bc_id,
                "modifier_type": row.get("type"),
                "required": bool(row.get("required")),
            }
            if existing:
                existing.write(vals)
            else:
                Modifier.create(vals)

    def _sync_images(self, image_rows):
        self.ensure_one()
        Image = self.env["bigcommerce.product.image"]
        for row in image_rows:
            bc_id = str(row.get("id"))
            existing = Image.search([
                ("bigcommerce_product_id", "=", self.id), ("bc_image_id", "=", bc_id),
            ], limit=1)
            vals = {
                "bigcommerce_product_id": self.id,
                "bc_image_id": bc_id,
                "image_url": row.get("url_standard") or row.get("url_zoom"),
                "is_thumbnail": bool(row.get("is_thumbnail")),
                "sort_order": row.get("sort_order") or 0,
            }
            if existing:
                existing.write(vals)
            else:
                existing = Image.create(vals)

        if self.config_id.download_images:
            # Pull the actual bytes down so images are visible in Odoo and
            # survive the BigCommerce CDN URL rotating later.
            self.image_ids.fetch_image_data()

        thumbnail = self.image_ids.filtered("is_thumbnail")[:1] or self.image_ids[:1]
        if thumbnail and self.variant_ids:
            thumbnail.apply_as_main_image()


    def job_push_fields(self, body=None, update_type=None, run_id=None, old=None, new=None):
        """Push a field payload for this product to BigCommerce.

        The single unit of work the deferred queue executes for a product-level
        bulk update. Also called directly when the queue is switched off, so it
        must own the whole step: request, local write-back, and audit log.
        """
        self.ensure_one()
        body = body or {}
        Log = self.env["bigcommerce.update.log"]
        config = self.config_id
        try:
            config._request("PUT", f"catalog/products/{self.bc_product_id}",
                            version="v3", json_body=body)
        except Exception as exc:  # noqa: BLE001
            Log.log_change(config, run_id, update_type, self.variant_ids[:1],
                           old, new, "error", str(exc))
            raise
        if "is_visible" in body:
            self.is_visible = body["is_visible"]
        Log.log_change(config, run_id, update_type, self.variant_ids[:1], old, new, "success")
        return True


class BigcommerceProductVariant(models.Model):
    _name = "bigcommerce.product.variant"
    _description = "BigCommerce Product Variant"
    _rec_name = "sku"

    bigcommerce_product_id = fields.Many2one("bigcommerce.product", required=True, ondelete="cascade")
    config_id = fields.Many2one(related="bigcommerce_product_id.config_id", store=True)
    bc_variant_id = fields.Char(string="BigCommerce Variant ID", required=True)
    sku = fields.Char()
    option_summary = fields.Char(string="Options", help="e.g. Color: Red, Size: M")
    product_id = fields.Many2one("product.product", string="Odoo Product")
    price = fields.Float()
    sale_price = fields.Float()
    inventory_level = fields.Integer()
    weight = fields.Float()

    _bc_variant_uniq = models.Constraint(
        "unique(bigcommerce_product_id, bc_variant_id)",
        "A BigCommerce variant can only be mirrored once.",
    )

    @classmethod
    def _sync_one(cls, bc_product, row):
        env = bc_product.env
        Variant = env["bigcommerce.product.variant"]
        Template = env["product.template"]
        bc_id = str(row.get("id"))
        sku = row.get("sku") or bc_product.sku or bc_id

        existing = Variant.search([
            ("bigcommerce_product_id", "=", bc_product.id), ("bc_variant_id", "=", bc_id),
        ], limit=1)

        odoo_product = existing.product_id
        if not odoo_product:
            template = Template.search([("default_code", "=", sku)], limit=1)
            if not template and bc_product.config_id.auto_create_product:
                option_values = row.get("option_values") or []
                name = bc_product.name
                if option_values:
                    name = "%s (%s)" % (name, ", ".join(
                        ov.get("label", "") for ov in option_values if ov.get("label")))
                template = Template.create({
                    "name": name,
                    "default_code": sku,
                    "type": "consu",
                    "is_storable": True,
                    "list_price": row.get("price") or 0.0,
                    "standard_price": row.get("cost_price") or 0.0,
                    "weight": row.get("weight") or 0.0,
                    "categ_id": bc_product.category_ids[:1].category_id.id
                    if bc_product.category_ids[:1].category_id else 1,
                })
            odoo_product = template.product_variant_id if template else False

        option_values = row.get("option_values") or []
        option_summary = ", ".join(
            f"{ov.get('option_display_name', '')}: {ov.get('label', '')}" for ov in option_values
        ) if option_values else False

        vals = {
            "bigcommerce_product_id": bc_product.id,
            "bc_variant_id": bc_id,
            "sku": sku,
            "option_summary": option_summary,
            "product_id": odoo_product.id if odoo_product else False,
            "price": row.get("price") or 0.0,
            "sale_price": row.get("sale_price") or 0.0,
            "inventory_level": row.get("inventory_level") or 0,
            "weight": row.get("weight") or 0.0,
        }
        if existing:
            existing.write(vals)
        else:
            existing = Variant.create(vals)

        # The mirror record above always tracked BigCommerce's price, but
        # nothing propagated it onto the real Odoo product on updates (only
        # ever set once, at creation) — a price change on BigCommerce, via
        # cron sync or webhook, silently never reached the sellable product.
        if odoo_product:
            effective_price = vals["sale_price"] or vals["price"]
            product_vals = {}
            if effective_price and odoo_product.lst_price != effective_price:
                product_vals["list_price"] = effective_price
            if vals["weight"] and odoo_product.weight != vals["weight"]:
                product_vals["weight"] = vals["weight"]
            if product_vals:
                odoo_product.product_tmpl_id.write(product_vals)

        existing._sync_stock_quantity()
        return existing

    def _sync_stock_quantity(self):
        self.ensure_one()
        if not self.product_id or self.bigcommerce_product_id.inventory_tracking == "none":
            return
        config = self.config_id
        quant = self.env["stock.quant"].sudo().search([
            ("product_id", "=", self.product_id.id),
            ("location_id", "=", config.warehouse_id.lot_stock_id.id),
        ], limit=1)
        if quant:
            quant.inventory_quantity = self.inventory_level
            quant.action_apply_inventory()
        else:
            self.env["stock.quant"].sudo().create({
                "product_id": self.product_id.id,
                "location_id": config.warehouse_id.lot_stock_id.id,
                "inventory_quantity": self.inventory_level,
            }).action_apply_inventory()


    def job_push_fields(self, body=None, update_type=None, run_id=None, old=None, new=None):
        """Push a field payload for this variant to BigCommerce.

        See BigcommerceProduct.job_push_fields - same contract, variant endpoint.
        """
        self.ensure_one()
        body = body or {}
        Log = self.env["bigcommerce.update.log"]
        config = self.config_id
        product_id = self.bigcommerce_product_id.bc_product_id
        try:
            config._request(
                "PUT", f"catalog/products/{product_id}/variants/{self.bc_variant_id}",
                version="v3", json_body=body)
        except Exception as exc:  # noqa: BLE001
            Log.log_change(config, run_id, update_type, self, old, new, "error", str(exc))
            raise
        for fname, key in (("price", "price"), ("sale_price", "sale_price"),
                           ("inventory_level", "inventory_level"), ("weight", "weight")):
            if key in body:
                self[fname] = body[key]
        Log.log_change(config, run_id, update_type, self, old, new, "success")
        return True


class BigcommerceProductModifier(models.Model):
    _name = "bigcommerce.product.modifier"
    _description = "BigCommerce Product Modifier"
    _rec_name = "name"

    bigcommerce_product_id = fields.Many2one("bigcommerce.product", required=True, ondelete="cascade")
    bc_modifier_id = fields.Char(required=True)
    name = fields.Char(required=True)
    modifier_type = fields.Char()
    required = fields.Boolean()


class BigcommerceProductImage(models.Model):
    _name = "bigcommerce.product.image"
    _description = "BigCommerce Product Image"
    _order = "sort_order"

    bigcommerce_product_id = fields.Many2one("bigcommerce.product", required=True, ondelete="cascade")
    bc_image_id = fields.Char(required=True)
    image_url = fields.Char()
    image_data = fields.Image(
        string="Image", max_width=1920, max_height=1920, verify_resolution=False,
        help="The actual image file, downloaded from BigCommerce during sync.",
    )
    fetched_url = fields.Char(
        readonly=True, copy=False,
        help="The URL image_data was downloaded from — used to avoid re-downloading "
             "an image that hasn't changed.",
    )
    is_thumbnail = fields.Boolean()
    sort_order = fields.Integer()

    def fetch_image_data(self, force=False):
        """Download the image bytes into image_data.

        Skips work when we already hold the bytes for this exact URL, so a
        re-sync of a large catalogue doesn't re-download every image. Never
        raises — a missing image must not fail a catalogue sync.
        """
        for image in self:
            if not image.image_url:
                continue
            if not force and image.image_data and image.fetched_url == image.image_url:
                continue
            try:
                response = requests.get(image.image_url, timeout=20)
                response.raise_for_status()
            except requests.RequestException as exc:
                _logger.warning("Could not fetch BigCommerce image %s: %s", image.image_url, exc)
                continue
            image.write({
                "image_data": base64.b64encode(response.content),
                "fetched_url": image.image_url,
            })
        return True

    def apply_as_main_image(self):
        """Put this image on the linked Odoo product(s) as their main image."""
        self.ensure_one()
        self.fetch_image_data()
        if not self.image_data:
            return
        for variant in self.bigcommerce_product_id.variant_ids:
            if variant.product_id and not variant.product_id.image_1920:
                variant.product_id.product_tmpl_id.image_1920 = self.image_data
