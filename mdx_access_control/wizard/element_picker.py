"""Pick buttons, tabs, reports and actions from what a model's views contain.

Hiding an element needs its technical name, which nobody knows by heart. This
wizard reads the model's own views and bindings and offers what it finds.
"""

from odoo import fields, models
from odoo.exceptions import UserError

from ..models.access_rules import ELEMENT_KINDS

VIEW_TYPES = ("form", "list", "kanban")


class MdxAccessElementPicker(models.TransientModel):
    _name = "mdx.access.element.picker"
    _description = "Pick Elements to Hide"

    profile_id = fields.Many2one("mdx.access.profile", required=True, ondelete="cascade")
    model_id = fields.Many2one(
        "ir.model", string="Model", ondelete="cascade", domain=[("transient", "=", False)])
    line_ids = fields.One2many("mdx.access.element.picker.line", "picker_id", string="Elements")
    scanned = fields.Boolean(readonly=True)

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Pick Buttons, Tabs, Reports"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_scan(self):
        self.ensure_one()
        if not self.model_id:
            raise UserError(self.env._("Choose a model first."))
        self.line_ids.unlink()
        already = {
            (rule.kind, rule.name)
            for rule in self.profile_id.element_rule_ids
            if rule.model_id == self.model_id
        }
        found = self._scan(self.model_id.model)
        self.write({
            "scanned": True,
            "line_ids": [(0, 0, {
                "kind": kind, "name": name, "label": label,
                "already_hidden": (kind, name) in already,
            }) for (kind, name), label in sorted(found.items())],
        })
        return self._reopen()

    def _scan(self, model_name):
        """``{(kind, name): label}`` for everything hideable on the model."""
        found = {}
        View = self.env["ir.ui.view"].sudo()
        views = View.search([
            ("model", "=", model_name), ("type", "in", VIEW_TYPES), ("inherit_id", "=", False)])
        for view in views:
            try:
                arch = view._get_combined_arch()
            except Exception:  # noqa: BLE001 - one broken view must not hide the rest
                continue
            for node in arch.iter("button"):
                name = node.get("name")
                if not name:
                    continue
                label = (node.get("string") or node.get("title") or (node.text or "").strip()
                         or node.get("aria-label") or name)
                found.setdefault(("button", name), "%s [%s]" % (label, view.type))
            for node in arch.iter("page"):
                name = node.get("name")
                if not name:
                    continue
                found.setdefault(("page", name), node.get("string") or name)

        bindings = self.env["ir.actions.actions"].sudo().get_bindings(model_name)
        for kind in ("report", "action"):
            for action in bindings.get(kind, ()):
                found.setdefault((kind, str(action["id"])), action.get("name") or str(action["id"]))
        return found

    def action_add(self):
        self.ensure_one()
        chosen = self.line_ids.filtered(lambda line: line.selected and not line.already_hidden)
        if not chosen:
            raise UserError(self.env._("Tick at least one element to hide."))
        self.env["mdx.access.element.rule"].create([{
            "profile_id": self.profile_id.id,
            "model_id": self.model_id.id,
            "kind": line.kind,
            "name": line.name,
            "label": line.label,
        } for line in chosen])
        return {"type": "ir.actions.act_window_close"}


class MdxAccessElementPickerLine(models.TransientModel):
    _name = "mdx.access.element.picker.line"
    _description = "Element Offered by the Picker"
    _order = "kind, label, id"

    picker_id = fields.Many2one("mdx.access.element.picker", required=True, ondelete="cascade")
    selected = fields.Boolean(string="Hide")
    kind = fields.Selection(ELEMENT_KINDS, readonly=True)
    name = fields.Char(string="Technical Name", readonly=True)
    label = fields.Char(readonly=True)
    already_hidden = fields.Boolean(readonly=True)
