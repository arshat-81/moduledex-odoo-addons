"""The three kinds of rule a profile carries."""

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools.safe_eval import safe_eval

from .access_profile import DOMAIN_NOTHING

# Models a rule may never target. ir.access refuses domains on itself, and
# restricting the module's own configuration is how an administrator who ticked
# "also restrict administrators" would lock the door from the inside.
PROTECTED_MODELS = (
    "ir.access",
    "mdx.access.profile",
    "mdx.access.model.rule",
    "mdx.access.field.rule",
    "mdx.access.element.rule",
)

ELEMENT_KINDS = [
    ("button", "Button"),
    ("page", "Tab"),
    ("report", "Report (Print menu)"),
    ("action", "Action (Action menu)"),
]


class MdxAccessRuleMixin(models.AbstractModel):
    """Shared plumbing: every rule change has to reach the enforcement layer."""
    _name = "mdx.access.rule.mixin"
    _description = "Access Rule"

    profile_id = fields.Many2one(
        "mdx.access.profile", string="Profile", required=True, ondelete="cascade", index=True)
    model_id = fields.Many2one(
        "ir.model", string="Model", required=True, ondelete="cascade", index=True,
        domain=[("transient", "=", False)])
    model_name = fields.Char(related="model_id.model", store=True, string="Technical Model")

    @api.constrains("model_id")
    def _check_protected_model(self):
        for rule in self:
            if rule.model_id.model in PROTECTED_MODELS:
                raise ValidationError(self.env._(
                    "%s cannot be restricted: it holds the access configuration itself.",
                    rule.model_id.model))

    @api.model_create_multi
    def create(self, vals_list):
        rules = super().create(vals_list)
        rules.profile_id._refresh_enforcement()
        return rules

    def write(self, vals):
        profiles = self.profile_id
        result = super().write(vals)
        (profiles | self.profile_id)._refresh_enforcement()
        return result

    def unlink(self):
        profiles = self.profile_id
        result = super().unlink()
        profiles._refresh_enforcement()
        return result


class MdxAccessModelRule(models.Model):
    _name = "mdx.access.model.rule"
    _description = "Model Access Rule"
    _inherit = ["mdx.access.rule.mixin"]
    _order = "profile_id, model_name, id"

    no_create = fields.Boolean(string="Block Create")
    no_write = fields.Boolean(string="Block Edit")
    no_unlink = fields.Boolean(string="Block Delete")
    read_domain = fields.Char(
        string="Can Read Only",
        help="Domain limiting which records the profile can see, e.g. "
             "[('user_id', '=', user.id)]. Leave empty for no limit. You can use user, "
             "company_id, company_ids and time.")
    write_domain = fields.Char(
        string="Can Edit Only",
        help="Domain limiting which records the profile can edit. Ignored when Edit is blocked.")
    unlink_domain = fields.Char(
        string="Can Delete Only",
        help="Domain limiting which records the profile can delete. Ignored when Delete is "
             "blocked.")
    hide_chatter = fields.Boolean(
        string="Hide Chatter",
        help="Remove the messages, notes and activities panel from this model's form.")
    access_ids = fields.One2many(
        "ir.access", "mdx_model_rule_id", string="Generated Restrictions", readonly=True)
    summary = fields.Char(compute="_compute_summary")

    @api.depends("no_create", "no_write", "no_unlink", "read_domain", "write_domain",
                 "unlink_domain", "hide_chatter")
    def _compute_summary(self):
        for rule in self:
            parts = []
            blocked = [label for flag, label in (
                (rule.no_create, self.env._("create")),
                (rule.no_write, self.env._("edit")),
                (rule.no_unlink, self.env._("delete")),
            ) if flag]
            if blocked:
                parts.append(self.env._("cannot %s", ", ".join(blocked)))
            if rule._clean(rule.read_domain):
                parts.append(self.env._("reads a subset"))
            if rule._clean(rule.write_domain) and not rule.no_write:
                parts.append(self.env._("edits a subset"))
            if rule._clean(rule.unlink_domain) and not rule.no_unlink:
                parts.append(self.env._("deletes a subset"))
            if rule.hide_chatter:
                parts.append(self.env._("no chatter"))
            rule.summary = "; ".join(parts) or self.env._("no restriction")

    @staticmethod
    def _clean(domain):
        domain = (domain or "").strip()
        return "" if domain in ("", "[]") else domain

    @api.constrains("read_domain", "write_domain", "unlink_domain", "model_id")
    def _check_domains(self):
        """Refuse a domain the server could not evaluate.

        An invalid domain on an ir.access restriction does not fail safe - it
        fails every request that touches the model. Catch it here, on save.
        """
        context = self.env["ir.access"]._eval_context()
        for rule in self:
            if rule.model_id.model not in self.env:
                continue
            model = self.env[rule.model_id.model].sudo()
            for label, text in ((self.env._("Can Read Only"), rule.read_domain),
                                (self.env._("Can Edit Only"), rule.write_domain),
                                (self.env._("Can Delete Only"), rule.unlink_domain)):
                text = self._clean(text)
                if not text:
                    continue
                try:
                    value = safe_eval(text, context)
                    if not isinstance(value, (list, tuple)):
                        raise ValueError(self.env._("it must be a list"))
                    Domain(value).validate(model)
                except Exception as error:  # noqa: BLE001 - any failure is a bad domain
                    raise ValidationError(self.env._(
                        "%(label)s on %(model)s is not a valid domain: %(error)s",
                        label=label, model=rule.model_id.model, error=error)) from error

    def _access_row_values(self, expression):
        """The ``ir.access`` restrictions this rule amounts to.

        Each row carries no group, which is what makes it a restriction, and a
        domain that is empty (no limit) for everybody outside the profile.
        """
        self.ensure_one()
        rows = []

        def row(operation, domain, what):
            rows.append({
                # Deliberately not translated: the name is part of what the sync
                # compares, and it must not change with the language of whoever
                # happens to save the profile.
                "name": "%s: %s %s" % (self.profile_id.name, self.model_id.model, what),
                "model_id": self.model_id.id,
                "group_id": False,
                "operation": operation,
                "domain": "%s if (%s) else []" % (domain, expression),
                "mdx_profile_id": self.profile_id.id,
                "mdx_model_rule_id": self.id,
            })

        blocked = "".join(letter for flag, letter in (
            (self.no_create, "c"), (self.no_write, "u"), (self.no_unlink, "d")) if flag)
        if blocked:
            row(blocked, DOMAIN_NOTHING, "blocked")
        read_domain = self._clean(self.read_domain)
        if read_domain:
            row("r", read_domain, "read filter")
        write_domain = self._clean(self.write_domain)
        if write_domain and not self.no_write:
            row("u", write_domain, "edit filter")
        unlink_domain = self._clean(self.unlink_domain)
        if unlink_domain and not self.no_unlink:
            row("d", unlink_domain, "delete filter")
        return rows


class MdxAccessFieldRule(models.Model):
    _name = "mdx.access.field.rule"
    _description = "Field Access Rule"
    _inherit = ["mdx.access.rule.mixin"]
    _order = "profile_id, model_name, field_name, id"

    field_id = fields.Many2one(
        "ir.model.fields", string="Field", required=True, ondelete="cascade",
        domain="[('model_id', '=', model_id)]")
    field_name = fields.Char(related="field_id.name", store=True, string="Technical Field")
    invisible = fields.Boolean(
        string="Hidden",
        help="Hidden in every view, and removed from filters, grouping, sorting and the export "
             "list.")
    readonly = fields.Boolean(string="Read-only")
    required = fields.Boolean(string="Required")
    strict = fields.Boolean(
        string="Reject Writes on Server",
        help="Also refuse any write to this field by the profile's members, whatever sends it. "
             "Careful: buttons and automations that write the field on the user's behalf are "
             "refused too. Leave it off unless the field must really be untouchable.")

    @api.constrains("field_id", "invisible", "readonly", "required", "strict")
    def _check_flags(self):
        for rule in self:
            if not (rule.invisible or rule.readonly or rule.required or rule.strict):
                raise ValidationError(self.env._(
                    "Choose what to do with %s: hide it, make it read-only or required.",
                    rule.field_id.name))
            if rule.required and (rule.invisible or rule.readonly):
                raise ValidationError(self.env._(
                    "%s cannot be required and hidden or read-only at once: nobody could "
                    "fill it in.", rule.field_id.name))

    @api.constrains("field_id", "model_id")
    def _check_field_model(self):
        for rule in self:
            if rule.field_id.model_id != rule.model_id:
                raise ValidationError(self.env._(
                    "%(field)s is not a field of %(model)s.",
                    field=rule.field_id.name, model=rule.model_id.model))


class MdxAccessElementRule(models.Model):
    _name = "mdx.access.element.rule"
    _description = "Hidden Button, Tab, Report or Action"
    _inherit = ["mdx.access.rule.mixin"]
    _order = "profile_id, model_name, kind, name, id"

    kind = fields.Selection(ELEMENT_KINDS, required=True, default="button")
    name = fields.Char(
        string="Technical Name", required=True,
        help="For a button or tab, its name in the view. For a report or action, its "
             "database id. Use Pick from Views rather than typing it.")
    label = fields.Char(help="What the user sees. Informational.")

    _unique_element = models.Constraint(
        "unique(profile_id, model_id, kind, name)",
        "That element is already hidden by this profile.",
    )
