"""The registry of protected fields."""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, ormcache

from . import vault_crypto

_logger = logging.getLogger(__name__)

# Only these can hold a credential; encrypting anything else would corrupt it.
SUPPORTED_TYPES = ("char", "text")


class MdxVaultField(models.Model):
    _name = "mdx.vault.field"
    _description = "Protected Credential Field"
    _order = "model_name, field_name"
    _rec_name = "display_label"

    display_label = fields.Char(compute="_compute_display_label", store=True)
    model_id = fields.Many2one(
        "ir.model", string="Model", required=True, ondelete="cascade",
        domain=[("transient", "=", False)],
    )
    model_name = fields.Char(related="model_id.model", store=True, string="Technical Model")
    field_id = fields.Many2one(
        "ir.model.fields", string="Field", required=True, ondelete="cascade",
        domain="[('model_id', '=', model_id), ('ttype', 'in', ['char', 'text']), "
               "('store', '=', True), ('related', '=', False)]",
    )
    field_name = fields.Char(related="field_id.name", store=True, string="Technical Field")
    active = fields.Boolean(
        default=True,
        help="Uncheck to stop encrypting new values. Values already encrypted stay encrypted "
             "and are still decrypted on read, so unchecking this cannot lock you out.",
    )
    encrypted_count = fields.Integer(
        string="Rows Encrypted", readonly=True,
        help="Rows whose stored value carries the vault marker, as of the last count.",
    )
    plaintext_count = fields.Integer(
        string="Rows Still in Plain Text", readonly=True,
        help="Rows holding a value that predates protection. Use Encrypt Existing Rows.",
    )
    last_counted_at = fields.Datetime(readonly=True)
    note = fields.Char(readonly=True)

    _model_field_uniq = models.Constraint(
        "unique(model_id, field_id)",
        "That field is already protected.",
    )

    @api.depends("model_name", "field_name")
    def _compute_display_label(self):
        for record in self:
            record.display_label = "%s.%s" % (record.model_name or "?", record.field_name or "?")

    # ------------------------------------------------------------------
    @api.constrains("field_id")
    def _check_field_is_storable(self):
        for record in self:
            field = record.field_id
            if not field:
                continue
            if field.ttype not in SUPPORTED_TYPES:
                raise UserError(_(
                    "Only text fields can be encrypted. %(field)s is a %(type)s.",
                    field=field.name, type=field.ttype))
            if not field.store:
                raise UserError(_(
                    "%s is not stored in the database, so there is nothing at rest to "
                    "protect.", field.name))
            model = self.env.get(record.model_name)
            if model is not None:
                odoo_field = model._fields.get(field.name)
                # A Char with a size limit will silently truncate ciphertext,
                # which destroys the secret. Refuse rather than corrupt data.
                if odoo_field is not None and getattr(odoo_field, "size", None):
                    raise UserError(_(
                        "%(field)s has a maximum length of %(size)s characters. Ciphertext is "
                        "longer than the value it replaces and would be truncated, destroying "
                        "the credential. Remove the size limit before protecting this field.",
                        field=field.name, size=odoo_field.size))

    def _invalidate(self):
        self.env.registry.clear_cache()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._invalidate()
        records.action_count()
        return records

    def write(self, vals):
        result = super().write(vals)
        self._invalidate()
        return result

    def unlink(self):
        result = super().unlink()
        self._invalidate()
        return result

    # ------------------------------------------------------------------
    @api.model
    @ormcache()
    def protected_map(self):
        """``{model_name: frozenset(field_names)}`` for every active entry.

        Consulted on every create, write and read through the ORM hook, so it is
        held in the registry cache. ``_invalidate`` calls
        ``registry.clear_cache()`` on every change to this table, which is what
        drops this entry - a hand-rolled dict on the registry would survive that
        call and go stale.
        """
        result = {}
        for record in self.sudo().search([("active", "=", True)]):
            if not record.model_name or not record.field_name:
                continue
            result.setdefault(record.model_name, set()).add(record.field_name)
        return {model: frozenset(names) for model, names in result.items()}

    # ------------------------------------------------------------------
    def action_count(self):
        """Count encrypted vs plaintext rows, reading the column directly.

        Deliberately raw SQL: going through the ORM would decrypt the values on
        the way out, which is exactly what must not happen while counting.
        """
        for record in self:
            model = self.env.get(record.model_name)
            if model is None or not model._auto:
                record.note = _("Model is not a stored table.")
                continue
            table = SQL.identifier(model._table)
            column = SQL.identifier(record.field_name)
            rows = self.env.execute_query(SQL(
                "SELECT %s FROM %s WHERE %s IS NOT NULL AND %s <> ''",
                column, table, column, column))
            encrypted = sum(1 for (value,) in rows if vault_crypto.is_encrypted(value))
            record.write({
                "encrypted_count": encrypted,
                "plaintext_count": len(rows) - encrypted,
                "last_counted_at": fields.Datetime.now(),
                "note": _("%s row(s) hold a value.", len(rows)),
            })
        return True

    def action_encrypt_existing(self):
        """Encrypt values written before this field was protected.

        Runs column-by-column in raw SQL for the same reason as the count, and
        skips anything already carrying the marker so it is safe to re-run.
        """
        for record in self:
            model = self.env.get(record.model_name)
            if model is None or not model._auto:
                raise UserError(_("%s is not a stored model.", record.model_name))
            table = SQL.identifier(model._table)
            column = SQL.identifier(record.field_name)
            rows = self.env.execute_query(SQL(
                "SELECT id, %s FROM %s WHERE %s IS NOT NULL AND %s <> ''",
                column, table, column, column))
            done = 0
            for row_id, value in rows:
                if vault_crypto.is_encrypted(value):
                    continue
                self.env.cr.execute(SQL(
                    "UPDATE %s SET %s = %s WHERE id = %s",
                    table, column, vault_crypto.encrypt(value), row_id))
                done += 1
            model.invalidate_model([record.field_name])
            record.action_count()
            record.note = _("Encrypted %s row(s) that were in plain text.", done)
            _logger.info("mdx_credential_vault: encrypted %s existing row(s) of %s.%s",
                         done, record.model_name, record.field_name)
        return True

    @api.model
    def action_check_key(self):
        """Surface key state, so 'is this actually on?' has a visible answer."""
        if vault_crypto.key_exists():
            message = _("Vault key present at %s.", vault_crypto.key_path())
        else:
            message = _("No vault key yet; one is created the first time a value is encrypted. "
                        "It will live at %s.", vault_crypto.key_path())
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Credential Vault"), "message": message, "sticky": False},
        }
