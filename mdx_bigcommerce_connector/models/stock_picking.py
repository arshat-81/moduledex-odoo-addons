import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    """Push Odoo deliveries back to BigCommerce as shipments.

    This closes the loop a merchant actually cares about: the order arrives from
    BigCommerce, it is picked and shipped in Odoo, and the shopper gets a
    tracking number from BigCommerce without anyone retyping it.
    """

    _inherit = "stock.picking"

    bigcommerce_shipment_id = fields.Char(readonly=True, copy=False, index=True,
                                          string="BigCommerce Shipment")
    bigcommerce_order_id = fields.Char(related="sale_id.bigcommerce_order_id",
                                       store=True, string="BigCommerce Order")
    bigcommerce_config_id = fields.Many2one(related="sale_id.bigcommerce_config_id", store=True)
    bigcommerce_sync_state = fields.Selection(
        [("na", "Not a BigCommerce order"), ("pending", "To push"),
         ("done", "Pushed"), ("error", "Failed")],
        compute="_compute_bigcommerce_sync_state", store=True, readonly=True)
    bigcommerce_sync_error = fields.Text(readonly=True, copy=False)

    @api.depends("bigcommerce_order_id", "bigcommerce_shipment_id",
                 "bigcommerce_sync_error", "picking_type_code")
    def _compute_bigcommerce_sync_state(self):
        for picking in self:
            if not picking.bigcommerce_order_id or picking.picking_type_code != "outgoing":
                picking.bigcommerce_sync_state = "na"
            elif picking.bigcommerce_shipment_id:
                picking.bigcommerce_sync_state = "done"
            elif picking.bigcommerce_sync_error:
                picking.bigcommerce_sync_state = "error"
            else:
                picking.bigcommerce_sync_state = "pending"

    # ── trigger ───────────────────────────────────────────────────────────

    def button_validate(self):
        res = super().button_validate()
        # Only after super(): a validation that raises (backorder wizard,
        # missing quantities) must not leave a shipment on BigCommerce for
        # goods that never actually left.
        for picking in self:
            if picking.state == "done":
                picking._bigcommerce_maybe_push_shipment()
        return res

    def _bigcommerce_maybe_push_shipment(self):
        self.ensure_one()
        config = self.bigcommerce_config_id
        if (not config or not config.auto_push_shipments
                or self.picking_type_code != "outgoing"
                or self.bigcommerce_shipment_id):
            return False
        config.dispatch(self, "job_push_shipment",
                        _("Shipment %(name)s -> BigCommerce order %(order)s",
                          name=self.name, order=self.bigcommerce_order_id))
        return True

    def action_push_shipment(self):
        """Manual push, from the delivery form."""
        for picking in self:
            if picking.state != "done":
                raise UserError(_("Validate the delivery before sending it to BigCommerce."))
            if not picking.bigcommerce_order_id:
                raise UserError(_("This delivery is not linked to a BigCommerce order."))
            picking.job_push_shipment()
        return True

    # ── the unit of work ──────────────────────────────────────────────────

    def job_push_shipment(self, run_id=None):
        """Create the shipment on BigCommerce for this delivery."""
        self.ensure_one()
        config = self.bigcommerce_config_id
        Log = self.env["bigcommerce.update.log"]
        if not config:
            raise UserError(_("No BigCommerce store on this delivery."))
        if self.bigcommerce_shipment_id:
            return True

        bc_order = self.bigcommerce_order_id
        try:
            items = self._bigcommerce_shipment_items(config, bc_order)
            if not items:
                raise UserError(_(
                    "None of the delivered products could be matched to a line on "
                    "BigCommerce order %(order)s.", order=bc_order))
            body = {
                "order_address_id": self._bigcommerce_address_id(config, bc_order),
                "items": items,
            }
            # carrier_tracking_ref / carrier_id are added by the optional
            # `stock_delivery` module; depending on it would force delivery-carrier
            # setup on every buyer, so read them only if they are present.
            tracking = (getattr(self, "carrier_tracking_ref", "") or "").strip()
            if tracking:
                body["tracking_number"] = tracking
            carrier_rec = getattr(self, "carrier_id", False)
            carrier = carrier_rec.name if carrier_rec else ""
            if carrier:
                # shipping_provider must be a provider BigCommerce knows; an
                # arbitrary carrier name is rejected, so send it as free text.
                body["shipping_provider"] = ""
                body["tracking_carrier"] = carrier
            if self.note:
                body["comments"] = self.env["ir.fields.converter"].text_from_html(self.note)

            created = config._request("POST", f"orders/{bc_order}/shipments",
                                      version="v2", json_body=body)
        except Exception as exc:  # noqa: BLE001
            self.sudo().write({"bigcommerce_sync_error": str(exc)})
            Log.log(config, "shipment_push", status="error",
                    message=_("Delivery %(name)s: %(error)s", name=self.name, error=str(exc)))
            raise

        self.sudo().write({
            "bigcommerce_shipment_id": str((created or {}).get("id") or ""),
            "bigcommerce_sync_error": False,
        })
        Log.log(config, "shipment_push", status="success",
                message=_("Delivery %(name)s sent to BigCommerce order %(order)s%(track)s.",
                          name=self.name, order=bc_order,
                          track=_(" with tracking %s") % tracking if tracking else ""))
        return True

    def _bigcommerce_address_id(self, config, bc_order):
        """BigCommerce needs the order's shipping-address id, not the customer's."""
        addresses = config._request("GET", f"orders/{bc_order}/shippingaddresses", version="v2")
        if not addresses:
            raise UserError(_("BigCommerce order %(order)s has no shipping address.",
                              order=bc_order))
        return addresses[0].get("id")

    def _bigcommerce_shipment_items(self, config, bc_order):
        """Map delivered quantities onto BigCommerce order_product_id values.

        A shipment references the ORDER LINE id, which is not the product or
        variant id, so the order's products have to be fetched and matched.
        """
        rows = config._request_all_pages(f"orders/{bc_order}/products", version="v2")
        by_variant, by_sku = {}, {}
        for row in rows:
            if row.get("variant_id"):
                by_variant[str(row["variant_id"])] = row
            if row.get("sku"):
                by_sku[str(row["sku"]).strip()] = row

        Variant = self.env["bigcommerce.product.variant"]
        items = []
        for move in self.move_ids.filtered(lambda m: m.state == "done" and m.quantity > 0):
            product = move.product_id
            row = None
            variant = Variant.search([
                ("config_id", "=", config.id), ("product_id", "=", product.id),
            ], limit=1)
            if variant and variant.bc_variant_id:
                row = by_variant.get(str(variant.bc_variant_id))
            if not row:
                sku = (product.default_code or "").strip()
                row = by_sku.get(sku) if sku else None
            if not row:
                _logger.warning("BigCommerce: delivery %s has %s, absent from order %s",
                                self.name, product.display_name, bc_order)
                continue
            # never claim to ship more than the order actually holds
            qty = min(int(move.quantity), int(row.get("quantity") or 0))
            if qty > 0:
                items.append({"order_product_id": row["id"], "quantity": qty})
        return items
