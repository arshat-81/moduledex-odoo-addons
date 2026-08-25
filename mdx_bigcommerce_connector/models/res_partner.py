import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    bigcommerce_customer_id = fields.Char(readonly=True, index=True)
    bigcommerce_customer_group_id = fields.Many2one("bigcommerce.customer.group", readonly=True)
    bigcommerce_config_id = fields.Many2one("bigcommerce.config", readonly=True)

    @classmethod
    def bigcommerce_sync_customers(cls, config):
        """Pull BigCommerce customers into res.partner. Matches an existing
        partner by BigCommerce id first, then by email, before creating a new
        one — so customers auto-created earlier from order billing addresses
        get adopted and linked rather than duplicated."""
        env = config.env
        Partner = env["res.partner"]
        Group = env["bigcommerce.customer.group"]

        rows = config._request_all_pages("customers")
        synced = Partner.browse()
        for data in rows:
            try:
                with env.cr.savepoint():
                    synced |= Partner._bigcommerce_upsert_customer(config, data, Group)
            except Exception as exc:  # noqa: BLE001 — one bad customer shouldn't kill the batch
                _logger.exception("BigCommerce customer sync failed for id=%s", data.get("id"))
                env["bigcommerce.update.log"].log(
                    config, "sync_customers", status="error",
                    message=f"Customer {data.get('id')} ({data.get('email')}): {exc}",
                )
        env["bigcommerce.update.log"].log(
            config, "sync_customers", record_count=len(synced))
        return synced

    @classmethod
    def _bigcommerce_upsert_customer(cls, config, data, Group=None):
        env = config.env
        Partner = env["res.partner"]
        Group = Group or env["bigcommerce.customer.group"]

        bc_id = str(data.get("id"))
        email = (data.get("email") or "").strip()

        partner = Partner.search([
            ("bigcommerce_config_id", "=", config.id),
            ("bigcommerce_customer_id", "=", bc_id),
        ], limit=1)
        if not partner and email:
            # Adopt a partner that already exists from an imported order.
            partner = Partner.search([("email", "=", email)], limit=1)

        group = Group.search([
            ("config_id", "=", config.id),
            ("bc_group_id", "=", str(data.get("customer_group_id") or "0")),
        ], limit=1)

        name = " ".join(p for p in [data.get("first_name"), data.get("last_name")] if p).strip()
        vals = {
            "name": name or email or bc_id,
            "email": email or False,
            "phone": data.get("phone") or False,
            "comment": data.get("notes") or False,
            "bigcommerce_customer_id": bc_id,
            "bigcommerce_config_id": config.id,
            "bigcommerce_customer_group_id": group.id if group else False,
        }
        if data.get("company"):
            vals["company_name"] = data["company"]
        if group and group.pricelist_id:
            vals["property_product_pricelist"] = group.pricelist_id.id

        if partner:
            partner.write(vals)
        else:
            partner = Partner.create(vals)
        partner._bigcommerce_sync_addresses(config, bc_id)
        return partner

    def _bigcommerce_sync_addresses(self, config, bc_customer_id):
        """Mirror BigCommerce customer addresses as Odoo child contacts."""
        self.ensure_one()
        Partner = self.env["res.partner"]
        Country = self.env["res.country"]
        try:
            rows = config._request_all_pages(
                "customers/addresses", params={"customer_id:in": bc_customer_id})
        except Exception:  # noqa: BLE001 — addresses are a nice-to-have, never fail the customer
            _logger.warning("Could not fetch BigCommerce addresses for customer %s", bc_customer_id)
            return

        for row in rows:
            country = Country.search([("code", "=", (row.get("country_iso2") or "").upper())], limit=1)
            addr_type = "invoice" if (row.get("address_type") or "").lower() == "billing" else "delivery"
            name = " ".join(p for p in [row.get("first_name"), row.get("last_name")] if p).strip()
            vals = {
                "parent_id": self.id,
                "type": addr_type,
                "name": name or self.name,
                "street": row.get("address1") or False,
                "street2": row.get("address2") or False,
                "city": row.get("city") or False,
                "zip": row.get("postal_code") or False,
                "phone": row.get("phone") or False,
                "country_id": country.id if country else False,
            }
            existing = Partner.search([
                ("parent_id", "=", self.id), ("type", "=", addr_type),
                ("street", "=", vals["street"]), ("zip", "=", vals["zip"]),
            ], limit=1)
            if existing:
                existing.write(vals)
            else:
                Partner.create(vals)
