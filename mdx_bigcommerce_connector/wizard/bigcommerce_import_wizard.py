import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BigcommerceImportWizard(models.TransientModel):
    _name = "bigcommerce.import.wizard"
    _description = "BigCommerce Targeted Import"

    config_id = fields.Many2one("bigcommerce.config", required=True,
                                default=lambda self: self.env["bigcommerce.config"].search(
                                    [("state", "=", "connected")], limit=1))
    entity = fields.Selection(
        [
            ("product", "Products"),
            ("order", "Orders"),
            ("customer", "Customers"),
            ("category", "Categories"),
            ("price_list", "Price Lists"),
        ],
        required=True, default="product",
    )
    mode = fields.Selection(
        [
            ("specific", "Specific BigCommerce IDs"),
            ("range", "ID range"),
            ("all", "Everything"),
        ],
        required=True, default="specific",
    )
    specific_ids = fields.Char(
        string="BigCommerce IDs",
        help="Comma-separated, e.g. 77, 80, 88 — import just these records.",
    )
    bc_id_from = fields.Char(string="From ID")
    bc_id_to = fields.Char(string="To ID")

    def _parse_specific_ids(self):
        raw = (self.specific_ids or "").replace(" ", "")
        if not raw:
            raise UserError(_("Enter at least one BigCommerce ID to import."))
        try:
            return [int(part) for part in raw.split(",") if part]
        except ValueError as exc:
            raise UserError(_("BigCommerce IDs must be numbers, separated by commas.")) from exc

    # ── per-entity fetchers ─────────────────────────────────────────────────
    def _fetch_products(self, config):
        params = {"include": "variants,images,modifiers,custom_fields"}
        if self.mode == "specific":
            params["id:in"] = ",".join(str(i) for i in self._parse_specific_ids())
        elif self.mode == "range":
            if self.bc_id_from:
                params["id:min"] = int(self.bc_id_from)
            if self.bc_id_to:
                params["id:max"] = int(self.bc_id_to)
        return config._request_all_pages("catalog/products", params=params)

    def _fetch_orders(self, config):
        if self.mode == "specific":
            # v2 orders has no id:in filter — fetch each one directly.
            rows = []
            for bc_id in self._parse_specific_ids():
                data = config._request("GET", f"orders/{bc_id}", version="v2")
                if data:
                    rows.append(data)
            return rows
        params = {}
        if self.mode == "range":
            if self.bc_id_from:
                params["min_id"] = int(self.bc_id_from)
            if self.bc_id_to:
                params["max_id"] = int(self.bc_id_to)
        return config._request_all_pages("orders", version="v2", params=params)

    def _fetch_customers(self, config):
        params = {}
        if self.mode == "specific":
            params["id:in"] = ",".join(str(i) for i in self._parse_specific_ids())
        elif self.mode == "range":
            if self.bc_id_from:
                params["id:min"] = int(self.bc_id_from)
            if self.bc_id_to:
                params["id:max"] = int(self.bc_id_to)
        return config._request_all_pages("customers", params=params)

    # ── import ──────────────────────────────────────────────────────────────
    def action_import(self):
        self.ensure_one()
        config = self.config_id
        env = self.env
        count = 0

        if self.entity == "product":
            rows = self._fetch_products(config)
            for row in rows:
                with env.cr.savepoint():
                    env["bigcommerce.product"]._sync_one(config, row)
                count += 1

        elif self.entity == "order":
            rows = self._fetch_orders(config)
            if not config.import_cancelled:
                rows = [r for r in rows if r.get("status_id") != 5]
            for row in rows:
                with env.cr.savepoint():
                    env["sale.order"]._bigcommerce_upsert(config, row)
                count += 1

        elif self.entity == "customer":
            rows = self._fetch_customers(config)
            for row in rows:
                with env.cr.savepoint():
                    env["res.partner"]._bigcommerce_upsert_customer(config, row)
                count += 1

        elif self.entity == "category":
            count = len(env["bigcommerce.category"].sync_from_bigcommerce(config))

        elif self.entity == "price_list":
            count = len(env["bigcommerce.price.list"].sync_from_bigcommerce(config))

        env["bigcommerce.update.log"].log(
            config, f"import:{self.entity}", record_count=count)

        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("BigCommerce Import"),
                "message": _("Imported %(count)s record(s).", count=count),
                "type": "success",
            },
        }
