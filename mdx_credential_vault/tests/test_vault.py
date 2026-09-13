"""The promise is narrow and testable: ciphertext in the column, plaintext through the ORM."""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import SQL

from odoo.addons.mdx_credential_vault.models import vault_crypto


@tagged("post_install", "-at_install")
class TestCredentialVault(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Vault = cls.env["mdx.vault.field"]
        # ir.mail_server.smtp_pass is the canonical plain-text credential in Odoo
        cls.model = cls.env["ir.model"]._get("ir.mail_server")
        assert cls.model, "fixture model ir.mail_server is missing"
        cls.field = cls.env["ir.model.fields"].search([
            ("model", "=", "ir.mail_server"), ("name", "=", "smtp_pass")], limit=1)
        assert cls.field, "fixture field ir.mail_server.smtp_pass is missing"

    def _protect(self):
        return self.Vault.create({"model_id": self.model.id, "field_id": self.field.id})

    def _raw(self, record):
        """Read the column directly, bypassing the decrypting ORM hook.

        Flushes first: the ORM defers UPDATEs, so a raw read straight after a
        write would see the pre-write column and report a false negative.
        """
        self.env.flush_all()
        rows = self.env.execute_query(SQL(
            "SELECT %s FROM %s WHERE id = %s",
            SQL.identifier("smtp_pass"), SQL.identifier("ir_mail_server"), record.id))
        return rows[0][0]

    # ------------------------------------------------------------------
    def test_crypto_roundtrip(self):
        token = vault_crypto.encrypt("hunter2")
        self.assertTrue(vault_crypto.is_encrypted(token))
        self.assertNotIn("hunter2", token, "the plaintext must not survive in the ciphertext")
        self.assertEqual(vault_crypto.decrypt(token), "hunter2")

    def test_encrypt_is_idempotent(self):
        once = vault_crypto.encrypt("s3cret")
        self.assertEqual(vault_crypto.encrypt(once), once,
                         "encrypting twice must not double-wrap")

    def test_plaintext_passes_through(self):
        self.assertEqual(vault_crypto.decrypt("not-encrypted"), "not-encrypted")
        self.assertEqual(vault_crypto.encrypt(""), "")
        self.assertEqual(vault_crypto.encrypt(False), False)

    # ------------------------------------------------------------------
    def test_write_stores_ciphertext_read_returns_plaintext(self):
        """The whole product, in one assertion pair."""
        self._protect()
        server = self.env["ir.mail_server"].create({
            "name": "MDX Vault Probe", "smtp_host": "localhost", "smtp_pass": "topsecret"})
        stored = self._raw(server)
        self.assertTrue(vault_crypto.is_encrypted(stored),
                        "the column must hold ciphertext")
        self.assertNotIn("topsecret", stored,
                         "a database dump must not reveal the credential")
        server.invalidate_recordset()
        self.assertEqual(server.smtp_pass, "topsecret",
                         "the owning module must still read its own password")

    def test_unprotected_field_is_untouched(self):
        server = self.env["ir.mail_server"].create({
            "name": "MDX Vault Control", "smtp_host": "localhost", "smtp_pass": "plainpass"})
        self.assertEqual(self._raw(server), "plainpass",
                         "without a registry entry nothing may be rewritten")

    def test_write_after_create_is_encrypted(self):
        self._protect()
        server = self.env["ir.mail_server"].create({
            "name": "MDX Vault Rewrite", "smtp_host": "localhost"})
        server.write({"smtp_pass": "changed-later"})
        self.assertTrue(vault_crypto.is_encrypted(self._raw(server)))
        server.invalidate_recordset()
        self.assertEqual(server.smtp_pass, "changed-later")

    def test_existing_plaintext_is_migrated_on_demand(self):
        server = self.env["ir.mail_server"].create({
            "name": "MDX Vault Legacy", "smtp_host": "localhost", "smtp_pass": "legacy-pw"})
        self.assertEqual(self._raw(server), "legacy-pw")

        entry = self._protect()
        entry.action_count()
        self.assertGreaterEqual(entry.plaintext_count, 1,
                                "the pre-existing value must be reported as plain text")
        server.invalidate_recordset()
        self.assertEqual(server.smtp_pass, "legacy-pw",
                         "an unmigrated value must stay readable, not break the integration")

        entry.action_encrypt_existing()
        self.assertTrue(vault_crypto.is_encrypted(self._raw(server)))
        server.invalidate_recordset()
        self.assertEqual(server.smtp_pass, "legacy-pw",
                         "migration must preserve the value it encrypts")

    def test_migration_is_safe_to_rerun(self):
        server = self.env["ir.mail_server"].create({
            "name": "MDX Vault Rerun", "smtp_host": "localhost", "smtp_pass": "once"})
        entry = self._protect()
        entry.action_encrypt_existing()
        first = self._raw(server)
        entry.action_encrypt_existing()
        self.assertEqual(self._raw(server), first,
                         "a second run must not re-encrypt what it already encrypted")
        server.invalidate_recordset()
        self.assertEqual(server.smtp_pass, "once")

    # ------------------------------------------------------------------
    def test_registry_rejects_a_sized_char(self):
        """Ciphertext is longer than the value; a size limit would truncate it."""
        sized = self.env["ir.model.fields"].search([
            ("model", "=", "res.country"), ("name", "=", "code")], limit=1)
        if not sized:
            self.skipTest("res.country.code not present")
        with self.assertRaises(UserError):
            self.Vault.create({
                "model_id": self.env["ir.model"]._get("res.country").id,
                "field_id": sized.id,
            })

    def test_protected_map_tracks_the_registry(self):
        self.assertNotIn("ir.mail_server", self.Vault.protected_map())
        entry = self._protect()
        self.assertIn("smtp_pass", self.Vault.protected_map().get("ir.mail_server", set()),
                      "adding an entry must invalidate the cached map")
        entry.active = False
        self.assertNotIn("ir.mail_server", self.Vault.protected_map(),
                         "deactivating must invalidate it too")
