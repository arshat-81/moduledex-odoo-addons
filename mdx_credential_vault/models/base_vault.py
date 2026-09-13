"""The ORM hook that makes encryption transparent.

Inheriting ``base`` puts these overrides in front of every model in the
registry, so the cost of the fast path matters more than anything else here.
The protected map is cached in the registry and the very first thing each
override does is a dict lookup that misses for essentially every model; only a
model that actually has a protected field does any further work.

Encryption happens on the way in (``create``/``write``) and decryption on the
way out (``_read_format``, which both ``read()`` and the web client's read path
funnel through). Nothing else in Odoo sees a difference: the field still behaves
like the Char it always was, and code that reads a password to open an FTP
connection keeps getting the password.

What this deliberately does NOT do is add a second permission layer on top of
the field's own access rules. Masking the value for some users would break every
integration that legitimately reads it, and the property being sold here is
"unreadable in a database dump", not "unreadable by a colleague".
"""

import threading

from odoo import api, models

from . import vault_crypto


# Set while the protected-field map is being computed, to stop the ORM reads it
# performs from re-entering the hook that asked for it.
_BUILDING = threading.local()


class Base(models.AbstractModel):
    _inherit = "base"

    @api.model
    def _vault_protected_fields(self):
        """Field names protected on this model, or an empty frozenset.

        The re-entrancy guard is not optional. Building the map runs a search on
        mdx.vault.field and reads ir.model / ir.model.fields through it, and each
        of those reads comes straight back through this hook - which recursed
        until the stack ran out. While the map is being built, every model
        reports "nothing protected", which is correct: the registry rows
        themselves never hold credentials.
        """
        if getattr(_BUILDING, "active", False):
            return frozenset()
        registry_model = self.env.get("mdx.vault.field")
        if registry_model is None:
            # the table does not exist yet - every model loaded before this one
            # during installation passes through here
            return frozenset()
        _BUILDING.active = True
        try:
            mapping = registry_model.protected_map()
        finally:
            _BUILDING.active = False
        return mapping.get(self._name, frozenset())

    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        protected = self._vault_protected_fields()
        if protected:
            for vals in vals_list:
                for name in protected & set(vals):
                    vals[name] = vault_crypto.encrypt(vals[name])
        return super().create(vals_list)

    def write(self, vals):
        protected = self._vault_protected_fields()
        if protected:
            for name in protected & set(vals):
                vals[name] = vault_crypto.encrypt(vals[name])
        return super().write(vals)

    def _fetch_query(self, query, fields):
        """Decrypt protected columns as they land in the ORM cache.

        This is the hook, not ``_read_format``. Reading ``record.smtp_pass`` in
        Python never calls ``read()`` - attribute access goes through the field
        descriptor to the prefetch, which fills the cache here. Decrypting in
        ``_read_format`` therefore served the web client and left every server-
        side caller holding ciphertext, which is the one thing that must not
        happen: the module that owns the credential has to keep working.

        Core documents this method as overridable precisely "to change the
        values that are put in cache".
        """
        fetched = super()._fetch_query(query, fields)
        protected = self._vault_protected_fields()
        if not protected:
            return fetched
        cache = self.env.cache
        for field in fields:
            if field.name not in protected:
                continue
            for record in fetched:
                value = cache.get(record, field, default=None)
                if vault_crypto.is_encrypted(value):
                    cache.set(record, field, vault_crypto.decrypt(value))
        return fetched


class IrModelFields(models.Model):
    """Drop the cached map when a protected field disappears with its module."""
    _inherit = "ir.model.fields"

    def unlink(self):
        result = super().unlink()
        self.env.registry.clear_cache()
        return result
