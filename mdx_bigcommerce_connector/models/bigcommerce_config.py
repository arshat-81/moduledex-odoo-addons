import logging
import time
import uuid

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

API_BASE = "https://api.bigcommerce.com/stores/{store_hash}/{version}/{path}"
REQUEST_TIMEOUT = 30
MAX_BACKOFF_SECONDS = 30


class BigcommerceConfig(models.Model):
    _name = "bigcommerce.config"
    _description = "BigCommerce Store Configuration"
    _rec_name = "name"

    name = fields.Char(required=True)
    store_hash = fields.Char(
        required=True, groups="mdx_bigcommerce_connector.group_bigcommerce_manager",
        help="From your BigCommerce API path: https://api.bigcommerce.com/stores/<store_hash>/v3/",
    )
    client_id = fields.Char(required=True, groups="mdx_bigcommerce_connector.group_bigcommerce_manager")
    access_token = fields.Char(required=True, groups="mdx_bigcommerce_connector.group_bigcommerce_manager")
    active = fields.Boolean(default=True)
    state = fields.Selection(
        [("draft", "Not Connected"), ("connected", "Connected"), ("error", "Error")],
        default="draft", readonly=True,
    )
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True)
    warehouse_id = fields.Many2one("stock.warehouse", required=True)
    pricelist_id = fields.Many2one("product.pricelist", string="Default Pricelist")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    auto_workflow = fields.Selection(
        [
            ("quotation", "Create Quotation"),
            ("sale", "Confirm Sale Order"),
            ("invoice", "Confirm + Auto Invoice"),
            ("paid", "Confirm + Invoice + Register Payment"),
        ],
        default="quotation", required=True,
    )
    auto_create_product = fields.Boolean(string="Auto-create missing products", default=True)
    download_images = fields.Boolean(
        string="Download product images", default=True,
        help="Store the actual image files in Odoo during sync. Turn off for very "
             "large catalogues if you only need the image URLs.",
    )
    auto_credit_note = fields.Boolean(string="Auto-create credit notes on refund", default=True)
    import_cancelled = fields.Boolean(string="Import cancelled orders", default=False)

    webhook_secret = fields.Char(
        default=lambda self: uuid.uuid4().hex, copy=False, readonly=True,
        groups="mdx_bigcommerce_connector.group_bigcommerce_manager",
    )
    webhook_base_url = fields.Char(compute="_compute_webhook_base_url")

    last_order_sync = fields.Datetime(readonly=True)
    last_product_sync = fields.Datetime(readonly=True)

    channel_ids = fields.One2many("bigcommerce.channel", "config_id", string="Storefronts")
    webhook_ids = fields.One2many("bigcommerce.webhook", "config_id", string="Webhooks")

    order_count = fields.Integer(compute="_compute_counts")
    product_count = fields.Integer(compute="_compute_counts")
    cart_count = fields.Integer(compute="_compute_counts")
    channel_count = fields.Integer(compute="_compute_counts")

    def _compute_counts(self):
        for config in self:
            config.order_count = self.env["sale.order"].search_count(
                [("bigcommerce_config_id", "=", config.id)])
            config.product_count = self.env["bigcommerce.product"].search_count(
                [("config_id", "=", config.id)])
            config.cart_count = self.env["bigcommerce.cart"].search_count(
                [("config_id", "=", config.id)])
            config.channel_count = len(config.channel_ids)

    @api.depends("webhook_secret")
    def _compute_webhook_base_url(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for config in self:
            config.webhook_base_url = (
                f"{base_url}/bigcommerce/webhook/{config.id}/{config.webhook_secret}"
                if config.id and config.webhook_secret else False
            )

    # ── credentials & HTTP client ───────────────────────────────────────────
    def _get_credentials(self):
        """Fresh cursor read, matching the pattern proven in the Shopify
        connector — avoids dead-cursor issues when called from long-running
        cron and webhook contexts."""
        self.ensure_one()
        try:
            new_cr = self.env.registry.cursor()
            try:
                new_cr.execute(
                    "SELECT store_hash, client_id, access_token FROM bigcommerce_config WHERE id = %s",
                    (self.id,),
                )
                row = new_cr.fetchone()
            finally:
                new_cr.close()
        except Exception as exc:
            raise UserError(_("Cannot read store credentials: %(error)s", error=str(exc))) from exc
        if not row:
            raise UserError(_("Store config not found."))
        return row[0], row[1], row[2]

    def _request(self, method, path, version="v3", params=None, json_body=None, _retry=True):
        self.ensure_one()
        store_hash, client_id, token = self._get_credentials()
        url = API_BASE.format(store_hash=store_hash, version=version, path=path.lstrip("/"))
        headers = {
            "X-Auth-Token": token,
            "X-Auth-Client": client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = requests.request(
                method, url, headers=headers, params=params, json=json_body, timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise UserError(_("BigCommerce connection failed: %(error)s", error=str(exc))) from exc

        if response.status_code == 429 and _retry:
            reset_ms = int(response.headers.get("X-Rate-Limit-Time-Reset-Ms", 1000) or 1000)
            _logger.info("BigCommerce rate limit hit, backing off %sms", reset_ms)
            time.sleep(min(reset_ms / 1000.0, MAX_BACKOFF_SECONDS))
            return self._request(method, path, version=version, params=params, json_body=json_body, _retry=False)

        if response.status_code == 401:
            raise UserError(_(
                "401 Unauthorized — BigCommerce rejected the API credentials.\n"
                "Check the store hash, client ID and access token, and that the "
                "API account has the required scopes."
            ))
        if not response.ok:
            raise UserError(_(
                "BigCommerce API error %(status)s: %(body)s",
                status=response.status_code, body=response.text[:300],
            ))
        return response.json() if response.content else {}

    def _request_all_pages(self, path, version="v3", params=None, max_pages=50):
        """Page through a BigCommerce list endpoint. v3 wraps results as
        {"data": [...], "meta": {"pagination": {...}}}; v2 returns a bare
        JSON array and has to be paged blindly until an empty/short page."""
        self.ensure_one()
        params = dict(params or {})
        page_size = params.setdefault("limit", 250)
        results = []
        page = 1
        while page <= max_pages:
            params["page"] = page
            data = self._request("GET", path, version=version, params=params)
            if isinstance(data, list):
                chunk = data
                results.extend(chunk)
                if len(chunk) < page_size:
                    break
            else:
                chunk = data.get("data", [])
                results.extend(chunk)
                pagination = (data.get("meta") or {}).get("pagination") or {}
                if not chunk or pagination.get("current_page", page) >= pagination.get("total_pages", page):
                    break
            page += 1
        return results

    # ── actions ──────────────────────────────────────────────────────────────
    def action_test_connection(self):
        self.ensure_one()
        try:
            data = self._request("GET", "store", version="v2")
        except UserError:
            self.write({"state": "error"})
            raise
        self.write({"state": "connected"})
        domain = data.get("domain") if isinstance(data, dict) else self.store_hash
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connection Successful"),
                "message": _("Connected to BigCommerce store: %(domain)s", domain=domain or self.store_hash),
                "sticky": False, "type": "success",
            },
        }

    def action_sync_channels(self):
        self.ensure_one()
        return self.env["bigcommerce.channel"].sync_from_bigcommerce(self)

    def action_sync_categories(self):
        self.ensure_one()
        return self.env["bigcommerce.category"].sync_from_bigcommerce(self)

    def action_sync_customer_groups(self):
        self.ensure_one()
        return self.env["bigcommerce.customer.group"].sync_from_bigcommerce(self)

    def action_sync_customers(self):
        self.ensure_one()
        return self.env["res.partner"].bigcommerce_sync_customers(self)

    def action_sync_price_lists(self):
        self.ensure_one()
        return self.env["bigcommerce.price.list"].sync_from_bigcommerce(self)

    def action_sync_products(self):
        self.ensure_one()
        result = self.env["bigcommerce.product"].sync_from_bigcommerce(self)
        self.write({"last_product_sync": fields.Datetime.now()})
        return result

    def action_sync_orders(self):
        self.ensure_one()
        result = self.env["sale.order"].bigcommerce_sync_orders(self)
        self.write({"last_order_sync": fields.Datetime.now()})
        return result

    def action_sync_carts(self):
        self.ensure_one()
        self.env["bigcommerce.cart"].sync_from_bigcommerce(self)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Carts sync"),
                "message": _(
                    "BigCommerce doesn't provide a way to list carts in bulk — "
                    "carts only appear here in real time, via the abandoned/"
                    "converted webhooks, as shoppers actually hit them. "
                    "Register webhooks below to start capturing them."
                ),
                "sticky": True, "type": "warning",
            },
        }

    def action_sync_all(self):
        self.ensure_one()
        self.action_sync_channels()
        self.action_sync_categories()
        self.action_sync_customer_groups()
        self.action_sync_price_lists()
        self.action_sync_customers()
        self.action_sync_products()
        self.action_sync_orders()
        # Carts are deliberately excluded here — BigCommerce has no bulk
        # cart-listing endpoint, so "sync carts" is a manual, explanatory
        # action rather than something to silently run on every full sync.

    def action_register_webhooks(self):
        self.ensure_one()
        return self.env["bigcommerce.webhook"].register_all(self)

    def action_view_orders(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Orders — %s", self.name),
            "res_model": "sale.order", "view_mode": "list,form",
            "domain": [("bigcommerce_config_id", "=", self.id)],
        }

    def action_view_products(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Products — %s", self.name),
            "res_model": "bigcommerce.product", "view_mode": "list,form",
            "domain": [("config_id", "=", self.id)],
        }

    def action_view_carts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Carts — %s", self.name),
            "res_model": "bigcommerce.cart", "view_mode": "list,form",
            "domain": [("config_id", "=", self.id)],
        }

    def action_view_dashboard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client", "tag": "bigcommerce_sales_dashboard",
            "name": _("Sales Dashboard — %s", self.name),
            "context": {"default_config_id": self.id},
        }

    def action_view_product_dashboard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client", "tag": "bigcommerce_catalog_dashboard",
            "name": _("Catalog Dashboard — %s", self.name),
            "context": {"default_config_id": self.id},
        }
