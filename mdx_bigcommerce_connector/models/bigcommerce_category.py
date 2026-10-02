from odoo import fields, models


class BigcommerceCategory(models.Model):
    _name = "bigcommerce.category"
    _description = "BigCommerce Category"
    _rec_name = "name"
    _order = "name"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    bc_category_id = fields.Char(string="BigCommerce Category ID", required=True)
    name = fields.Char(required=True)
    parent_bc_id = fields.Char(string="BigCommerce Parent ID")
    parent_id = fields.Many2one("bigcommerce.category")
    category_id = fields.Many2one(
        "product.category", string="Odoo Category",
        help="Odoo product category this maps to for products in this BigCommerce category.",
    )
    channel_ids = fields.Many2many("bigcommerce.channel", string="Visible on Storefronts")

    _bc_category_uniq = models.Constraint(
        "unique(config_id, bc_category_id)",
        "A BigCommerce category can only be mirrored once per store.",
    )

    @classmethod
    def sync_from_bigcommerce(cls, config):
        env = config.env
        Category = env["bigcommerce.category"]
        ProductCategory = env["product.category"]
        categories = config._request_all_pages("catalog/categories")
        synced = Category.browse()
        for data in categories:
            bc_id = str(data.get("id"))
            existing = Category.search([
                ("config_id", "=", config.id), ("bc_category_id", "=", bc_id),
            ], limit=1)
            odoo_category = existing.category_id
            if not odoo_category:
                odoo_category = ProductCategory.search([("name", "=", data.get("name"))], limit=1) \
                    or ProductCategory.create({"name": data.get("name") or bc_id})
            vals = {
                "config_id": config.id,
                "bc_category_id": bc_id,
                "name": data.get("name") or bc_id,
                "parent_bc_id": str(data.get("parent_id")) if data.get("parent_id") else False,
                "category_id": odoo_category.id,
            }
            if existing:
                existing.write(vals)
            else:
                existing = Category.create(vals)
            synced |= existing

        # Second pass: link parents now that every category in this batch
        # exists, regardless of the order the API returned them in.
        for category in synced.filtered("parent_bc_id"):
            parent = Category.search([
                ("config_id", "=", config.id), ("bc_category_id", "=", category.parent_bc_id),
            ], limit=1)
            if parent and category.parent_id != parent:
                category.parent_id = parent.id
        return synced
