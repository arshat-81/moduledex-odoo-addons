import hmac
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class BigcommerceWebhookController(http.Controller):

    @http.route(
        "/bigcommerce/webhook/<int:config_id>/<string:secret>",
        type="http", auth="public", methods=["POST"], csrf=False,
    )
    def handle_webhook(self, config_id, secret, **kwargs):
        config = request.env["bigcommerce.config"].sudo().browse(config_id).exists()
        if not config or not config.active or not hmac.compare_digest(
                config.sudo().webhook_secret or "", secret or ""):
            return request.make_json_response({"error": "invalid"}, status=403)

        try:
            payload = request.get_json_data()
        except ValueError:
            return request.make_json_response({"error": "bad_json"}, status=400)

        scope = payload.get("scope") or ""
        data = payload.get("data") or {}
        Log = request.env["bigcommerce.update.log"].sudo()
        _logger.info("BigCommerce webhook: scope=%s store=%s", scope, config.name)

        try:
            if scope.startswith("store/order/refund"):
                self._handle_refund(config, data)
            elif scope.startswith("store/order"):
                self._handle_order(config, data)
            elif scope.startswith("store/product"):
                self._handle_product(config, data)
            elif scope.startswith("store/cart"):
                self._handle_cart(config, scope, data)
            elif "deliveryException" in scope:
                self._handle_delivery_exception(config, scope, payload)
            else:
                _logger.info("BigCommerce webhook: unhandled scope %s", scope)
        except Exception as exc:  # noqa: BLE001 — never let a bad payload break the endpoint
            _logger.exception("BigCommerce webhook processing failed for scope %s", scope)
            Log.log(config, f"webhook:{scope}", status="error", message=str(exc))
            return request.make_json_response({"status": "error"}, status=200)

        Log.log(config, f"webhook:{scope}", status="success")
        return request.make_json_response({"status": "ok"})

    def _resource_id(self, data):
        return data.get("id") or data.get("orderId") or data.get("order_id")

    def _handle_order(self, config, data):
        order_id = self._resource_id(data)
        if not order_id:
            return
        order_data = config._request("GET", f"orders/{order_id}", version="v2")
        if order_data:
            request.env["sale.order"].sudo()._bigcommerce_upsert(config, order_data, source="webhook")

    def _handle_refund(self, config, data):
        order_id = data.get("orderId") or data.get("order_id")
        if not order_id:
            return
        order = request.env["sale.order"].sudo().search([
            ("bigcommerce_config_id", "=", config.id), ("bigcommerce_order_id", "=", str(order_id)),
        ], limit=1)
        if not order:
            order_data = config._request("GET", f"orders/{order_id}", version="v2")
            order = request.env["sale.order"].sudo()._bigcommerce_upsert(config, order_data, source="webhook")
        refund_payload = {
            "id": data.get("id"),
            "order_id": order_id,
            "amount": data.get("amount"),
            "reason": data.get("reason"),
        }
        order.sudo()._bigcommerce_handle_refund(config, refund_payload)

    def _handle_product(self, config, data):
        product_id = self._resource_id(data)
        if not product_id:
            return
        product_data = config._request(
            "GET", f"catalog/products/{product_id}", version="v3",
            params={"include": "variants,images,modifiers,custom_fields"},
        ).get("data")
        if product_data:
            request.env["bigcommerce.product"].sudo()._sync_one(config, product_data)

    def _handle_cart(self, config, scope, data):
        cart_id = self._resource_id(data)
        if not cart_id:
            return
        state = None
        if scope.endswith("abandoned"):
            state = "abandoned"
        elif scope.endswith("converted"):
            state = "converted"
        cart_data = config._request("GET", f"carts/{cart_id}", version="v3").get("data")
        if cart_data:
            request.env["bigcommerce.cart"].sudo()._sync_one(config, cart_data, state=state)

    def _handle_delivery_exception(self, config, scope, payload):
        webhook_bc_id = str((payload.get("data") or {}).get("webhookId") or "")
        webhook = request.env["bigcommerce.webhook"].sudo().search([
            ("config_id", "=", config.id), ("bc_webhook_id", "=", webhook_bc_id),
        ], limit=1)
        if webhook:
            exception_type = (payload.get("data") or {}).get("exceptionType", scope)
            webhook.mark_delivery_exception(exception_type, str(payload.get("data")))
