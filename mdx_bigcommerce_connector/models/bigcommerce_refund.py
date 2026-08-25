from odoo import _, fields, models
from odoo.exceptions import UserError


class BigcommerceRefund(models.Model):
    _name = "bigcommerce.refund"
    _description = "BigCommerce Refund"
    _rec_name = "bc_refund_id"

    config_id = fields.Many2one("bigcommerce.config", required=True, ondelete="cascade")
    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade")
    bc_refund_id = fields.Char(string="BigCommerce Refund ID")
    bc_order_id = fields.Char(string="BigCommerce Order ID", required=True)
    amount = fields.Float()
    reason = fields.Char()
    state = fields.Selection(
        [("quoted", "Quoted"), ("executed", "Executed"), ("failed", "Failed")],
        default="quoted",
    )
    credit_note_id = fields.Many2one("account.move", readonly=True, copy=False)

    def action_create_credit_note(self):
        """Generate an Odoo credit note for this refund and reconcile it
        against the order's posted invoice, if there is one."""
        for refund in self:
            if refund.credit_note_id:
                continue
            invoice = refund.order_id.invoice_ids.filtered(
                lambda m: m.move_type == "out_invoice" and m.state == "posted")[:1]
            if not invoice:
                raise UserError(_(
                    "No posted invoice found on order %(order)s to credit against.",
                    order=refund.order_id.name,
                ))
            credit_note = invoice._reverse_moves(
                default_values_list=[{
                    "invoice_origin": refund.order_id.name,
                    "ref": _("BigCommerce refund %(id)s", id=refund.bc_refund_id or refund.bc_order_id),
                }],
            )
            credit_note.action_post()
            refund.write({"credit_note_id": credit_note.id, "state": "executed"})
        return True

    @classmethod
    def create_from_webhook(cls, config, order, payload):
        env = config.env
        Refund = env["bigcommerce.refund"]
        refund = Refund.create({
            "config_id": config.id,
            "order_id": order.id,
            "bc_order_id": str(payload.get("order_id") or order.bigcommerce_order_id),
            "bc_refund_id": str(payload.get("id")) if payload.get("id") else False,
            "amount": float(payload.get("amount") or 0),
            "reason": payload.get("reason"),
            "state": "executed",
        })
        if config.auto_credit_note:
            try:
                refund.action_create_credit_note()
            except UserError:
                refund.state = "failed"
        return refund
