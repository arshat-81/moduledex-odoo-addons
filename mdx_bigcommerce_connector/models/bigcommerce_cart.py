from odoo import _, fields, models


class BigcommerceCart(models.Model):
    _name = "bigcommerce.cart"
    _description = "BigCommerce Cart"
    _rec_name = "bc_cart_id"
    _order = "updated_at desc"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    bc_cart_id = fields.Char(string="BigCommerce Cart ID", required=True)
    channel_id = fields.Many2one("bigcommerce.channel")
    email = fields.Char()
    partner_id = fields.Many2one("res.partner")
    currency_code = fields.Char()
    subtotal = fields.Float()
    line_ids = fields.One2many("bigcommerce.cart.line", "cart_id", string="Lines")
    line_count = fields.Integer(compute="_compute_line_count")
    state = fields.Selection(
        [("active", "Active"), ("abandoned", "Abandoned"), ("converted", "Converted")],
        default="active",
    )
    lead_id = fields.Many2one("crm.lead", readonly=True, copy=False)
    updated_at = fields.Datetime()

    _bc_cart_uniq = models.Constraint(
        "unique(config_id, bc_cart_id)",
        "A BigCommerce cart can only be mirrored once per store.",
    )

    def _compute_line_count(self):
        for cart in self:
            cart.line_count = len(cart.line_ids)

    @classmethod
    def sync_from_bigcommerce(cls, config):
        """BigCommerce has no bulk cart-listing endpoint at all — confirmed
        against the live API (a bare GET to v3/carts 404s; carts only exist
        at /v3/carts/{id}). There is nothing to poll or backfill. Cart
        tracking is entirely real-time, driven by the cart/abandoned and
        cart/converted webhooks in controllers/webhook.py, which fetch one
        specific cart by the id the webhook names."""
        config.env["bigcommerce.update.log"].log(
            config, "sync_carts", status="warning",
            message="BigCommerce has no cart list endpoint — carts are only captured live via webhooks.",
        )
        return config.env["bigcommerce.cart"].browse()

    @classmethod
    def _sync_one(cls, config, data, state=None):
        env = config.env
        Cart = env["bigcommerce.cart"]
        CartLine = env["bigcommerce.cart.line"]
        Partner = env["res.partner"]
        Channel = env["bigcommerce.channel"]

        bc_id = str(data.get("id"))
        existing = Cart.search([
            ("config_id", "=", config.id), ("bc_cart_id", "=", bc_id),
        ], limit=1)

        email = (data.get("email") or "").strip()
        partner = Partner.search([("email", "=", email)], limit=1) if email else Partner.browse()

        vals = {
            "config_id": config.id,
            "bc_cart_id": bc_id,
            "channel_id": Channel.search([
                ("config_id", "=", config.id), ("bc_channel_id", "=", str(data.get("channel_id"))),
            ], limit=1).id or False,
            "email": email or False,
            "partner_id": partner.id if partner else False,
            "currency_code": (data.get("currency") or {}).get("code"),
            "subtotal": float((data.get("cart_amount") or 0)),
            "updated_at": fields.Datetime.now(),
        }
        if state:
            vals["state"] = state
        if existing:
            existing.write(vals)
        else:
            existing = Cart.create(vals)

        existing.line_ids.unlink()
        for item_group in ("physical_items", "digital_items", "custom_items"):
            for line in (data.get("line_items") or {}).get(item_group, []):
                CartLine.create({
                    "cart_id": existing.id,
                    "name": line.get("name") or "",
                    "sku": line.get("sku") or "",
                    "quantity": line.get("quantity") or 1,
                    "list_price": (line.get("list_price") or 0),
                })
        return existing

    def action_convert_to_lead(self):
        """The one thing no competing BigCommerce connector offers: turn a
        recovered/abandoned cart straight into a CRM lead for follow-up."""
        Lead = self.env["crm.lead"]
        for cart in self:
            if cart.lead_id:
                continue
            description = "\n".join(
                f"{line.quantity} x {line.name} ({line.sku}) — {line.list_price}"
                for line in cart.line_ids
            )
            lead = Lead.create({
                "name": _("Abandoned BigCommerce cart — %s", cart.email or cart.bc_cart_id),
                "partner_id": cart.partner_id.id,
                "email_from": cart.email,
                "description": description,
                "expected_revenue": cart.subtotal,
                "type": "lead",
            })
            cart.write({"lead_id": lead.id, "state": "converted"})
        return {
            "type": "ir.actions.act_window",
            "res_model": "crm.lead",
            "view_mode": "list,form",
            "domain": [("id", "in", self.mapped("lead_id").ids)],
        }


class BigcommerceCartLine(models.Model):
    _name = "bigcommerce.cart.line"
    _description = "BigCommerce Cart Line"

    cart_id = fields.Many2one("bigcommerce.cart", required=True, ondelete="cascade")
    name = fields.Char()
    sku = fields.Char()
    quantity = fields.Float()
    list_price = fields.Float()
