import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# BigCommerce sends money as 4-decimal strings; anything under this is noise.
EPSILON = 0.005


class BigcommerceTaxMapping(models.Model):
    """Maps a BigCommerce tax rate to a real Odoo tax.

    BigCommerce does not expose "which tax record applied" on an order line -
    it only reports the amounts (`total_ex_tax`, `total_tax`) and an optional
    `tax_class_id`. The effective rate is therefore derived per line, and this
    model remembers which Odoo tax to use for it, so the choice is visible and
    can be corrected instead of being guessed silently on every import.
    """

    _name = "bigcommerce.tax.mapping"
    _description = "BigCommerce Tax Mapping"
    _order = "config_id, rate"

    config_id = fields.Many2one("bigcommerce.config", required=True,
                                ondelete="cascade", index=True)
    company_id = fields.Many2one(related="config_id.company_id", store=True)
    rate = fields.Float(string="BigCommerce Rate (%)", required=True, digits=(16, 4),
                        help="Effective rate seen on the order line: total_tax / total_ex_tax.")
    bc_tax_class_id = fields.Char(string="BC Tax Class",
                                  help="Reported by BigCommerce when the store uses tax classes.")
    account_tax_id = fields.Many2one(
        "account.tax", string="Odoo Tax", required=True, ondelete="restrict",
        domain="[('type_tax_use', '=', 'sale'), ('company_id', '=', company_id)]")
    auto_created = fields.Boolean(readonly=True,
                                  help="The Odoo tax was created by the connector, not chosen by a user.")
    order_count = fields.Integer(compute="_compute_order_count")

    _unique_rate = models.Constraint(
        "UNIQUE(config_id, rate, bc_tax_class_id)",
        "A BigCommerce rate can only be mapped once per store.")

    def _compute_order_count(self):
        for rec in self:
            rec.order_count = self.env["sale.order.line"].search_count(
                [("tax_ids", "in", rec.account_tax_id.ids)])

    @api.depends("rate", "account_tax_id")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%.4g%% -> %s" % (rec.rate, rec.account_tax_id.name or "?")

    # ── resolution ────────────────────────────────────────────────────────

    @api.model
    def resolve(self, config, rate, tax_class_id=None):
        """Return the account.tax for a BigCommerce rate, or an empty recordset.

        Order of preference: an existing mapping, then an existing Odoo sale tax
        with exactly that percentage, then (only if the store allows it) a newly
        created tax.
        """
        if rate <= 0:
            return self.env["account.tax"]

        rate = round(rate, 4)
        tax_class_id = str(tax_class_id) if tax_class_id not in (None, "") else False

        mapping = self.search([
            ("config_id", "=", config.id),
            ("rate", ">=", rate - 0.0001), ("rate", "<=", rate + 0.0001),
            ("bc_tax_class_id", "=", tax_class_id),
        ], limit=1)
        if not mapping and tax_class_id:
            # fall back to a rate-only mapping when the class is not mapped
            mapping = self.search([
                ("config_id", "=", config.id),
                ("rate", ">=", rate - 0.0001), ("rate", "<=", rate + 0.0001),
                ("bc_tax_class_id", "=", False),
            ], limit=1)
        if mapping:
            return mapping.account_tax_id

        Tax = self.env["account.tax"]
        existing = Tax.search([
            ("type_tax_use", "=", "sale"),
            ("company_id", "=", config.company_id.id),
            ("amount_type", "=", "percent"),
            ("amount", ">=", rate - 0.0001), ("amount", "<=", rate + 0.0001),
            ("price_include_override", "in", (False, "tax_excluded")),
        ], limit=1)

        if not existing:
            if not config.tax_auto_create:
                _logger.warning(
                    "BigCommerce: no Odoo sale tax at %.4g%% for store %s and auto-create "
                    "is off - the line will be imported untaxed.", rate, config.name)
                return Tax
            existing = Tax.sudo().create({
                "name": _("BigCommerce %(rate).4g%%", rate=rate),
                "amount": rate,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": config.company_id.id,
                "price_include_override": "tax_excluded",
            })
            _logger.info("BigCommerce: created Odoo tax %s for store %s", existing.name, config.name)

        self.sudo().create({
            "config_id": config.id,
            "rate": rate,
            "bc_tax_class_id": tax_class_id,
            "account_tax_id": existing.id,
            "auto_created": bool(config.tax_auto_create) and not existing.id,
        })
        return existing

    @api.model
    def line_rate(self, row):
        """Effective tax rate of one BigCommerce order-product row, as a percent."""
        try:
            total_ex = float(row.get("total_ex_tax") or 0.0)
            total_tax = float(row.get("total_tax") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if total_ex <= 0 or total_tax <= EPSILON:
            return 0.0
        return round(total_tax / total_ex * 100.0, 4)

    def action_view_taxes(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Odoo Tax"),
            "res_model": "account.tax",
            "res_id": self.account_tax_id.id,
            "view_mode": "form",
        }
