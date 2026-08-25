from odoo import fields, models


class BigcommerceCustomerGroup(models.Model):
    _name = "bigcommerce.customer.group"
    _description = "BigCommerce Customer Group"
    _rec_name = "name"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    bc_group_id = fields.Char(string="BigCommerce Group ID", required=True)
    name = fields.Char(required=True)
    is_default = fields.Boolean()
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Odoo Pricelist",
        help="Customers in this BigCommerce group get this Odoo pricelist on their sale orders.",
    )

    _bc_group_uniq = models.Constraint(
        "unique(config_id, bc_group_id)",
        "A BigCommerce customer group can only be mirrored once per store.",
    )

    @classmethod
    def sync_from_bigcommerce(cls, config):
        env = config.env
        Group = env["bigcommerce.customer.group"]
        groups = config._request_all_pages("customer_groups", version="v2")
        synced = Group.browse()
        for data in groups:
            bc_id = str(data.get("id"))
            existing = Group.search([
                ("config_id", "=", config.id), ("bc_group_id", "=", bc_id),
            ], limit=1)
            vals = {
                "config_id": config.id,
                "bc_group_id": bc_id,
                "name": data.get("name") or bc_id,
                "is_default": bool(data.get("is_default")),
            }
            if existing:
                existing.write(vals)
            else:
                existing = Group.create(vals)
            synced |= existing
        return synced
