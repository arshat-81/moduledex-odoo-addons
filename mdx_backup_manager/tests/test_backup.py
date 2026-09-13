"""Verification is the product, so most of these tests try to fool it."""

import hashlib
import io
import os
import shutil
import tempfile
import zipfile
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.mdx_backup_manager.models import backup_transport


def make_dump(extra=b"x" * 64, members=("dump.sql", "manifest.json")):
    """A byte-for-byte plausible Odoo zip dump."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in members:
            archive.writestr(name, extra)
    return buffer.getvalue()


@tagged("post_install", "-at_install")
class TestBackup(TransactionCase):

    def setUp(self):
        """A fresh directory per test.

        TransactionCase rolls back the database but not the filesystem, so a
        directory shared across tests accumulates every file the earlier ones
        wrote - which made the retention test count other tests' leftovers.
        """
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="mdx_backup_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dest = self.env["mdx.backup.destination"].create({
            "name": "Test Local",
            "destination_type": "local",
            "local_path": self.tmp,
            "db_name": self.env.cr.dbname,
            "with_filestore": False,
            "keep_count": 3,
        })

    # ------------------------------------------------------------------
    def test_local_transport_roundtrip(self):
        transport = backup_transport.get_transport(self.dest)
        transport.check()
        transport.put("a.zip", b"hello")
        self.assertEqual(transport.fetch("a.zip"), b"hello")
        self.assertIn("a.zip", [n for n, _ in transport.list()])
        transport.remove("a.zip")
        self.assertNotIn("a.zip", [n for n, _ in transport.list()])

    def test_transport_rejects_a_missing_folder(self):
        self.dest.local_path = os.path.join(self.tmp, "does-not-exist")
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            backup_transport.get_transport(self.dest).check()

    # ------------------------------------------------------------------
    def test_verify_accepts_a_good_archive(self):
        payload = make_dump()
        transport = backup_transport.get_transport(self.dest)
        transport.put("good.zip", payload)
        ok, detail = self.dest._verify(transport, "good.zip", payload,
                                       hashlib.sha256(payload).hexdigest())
        self.assertTrue(ok, detail)

    def test_verify_catches_a_truncated_upload(self):
        """The failure mode that matters: the transfer stopped half way."""
        payload = make_dump()
        transport = backup_transport.get_transport(self.dest)
        transport.put("short.zip", payload[:len(payload) // 2])
        ok, detail = self.dest._verify(transport, "short.zip", payload,
                                       hashlib.sha256(payload).hexdigest())
        self.assertFalse(ok)
        self.assertIn("read back", detail.lower())

    def test_verify_catches_a_silent_corruption(self):
        """Same length, different bytes - only the checksum catches this."""
        payload = make_dump()
        corrupted = bytearray(payload)
        corrupted[-1] ^= 0xFF
        transport = backup_transport.get_transport(self.dest)
        transport.put("bad.zip", bytes(corrupted))
        ok, detail = self.dest._verify(transport, "bad.zip", payload,
                                       hashlib.sha256(payload).hexdigest())
        self.assertFalse(ok)
        self.assertIn("checksum", detail.lower())

    def test_verify_rejects_an_archive_missing_dump_sql(self):
        payload = make_dump(members=("manifest.json",))
        transport = backup_transport.get_transport(self.dest)
        transport.put("partial.zip", payload)
        ok, detail = self.dest._verify(transport, "partial.zip", payload,
                                       hashlib.sha256(payload).hexdigest())
        self.assertFalse(ok)
        self.assertIn("dump.sql", detail)

    def test_verify_rejects_something_that_is_not_a_zip(self):
        payload = b"this is not a zip file at all"
        transport = backup_transport.get_transport(self.dest)
        transport.put("nope.zip", payload)
        ok, detail = self.dest._verify(transport, "nope.zip", payload,
                                       hashlib.sha256(payload).hexdigest())
        self.assertFalse(ok)
        self.assertIn("zip", detail.lower())

    def test_verify_reports_a_file_that_vanished(self):
        payload = make_dump()
        transport = backup_transport.get_transport(self.dest)
        ok, detail = self.dest._verify(transport, "never-written.zip", payload,
                                       hashlib.sha256(payload).hexdigest())
        self.assertFalse(ok)
        self.assertIn("could not be read back", detail)

    # ------------------------------------------------------------------
    def test_retention_keeps_the_newest_and_prunes_the_rest(self):
        Run = self.env["mdx.backup.run"]
        transport = backup_transport.get_transport(self.dest)
        now = fields.Datetime.now()
        for i in range(5):
            name = "old-%s.zip" % i
            transport.put(name, b"x")
            Run.create({
                "destination_id": self.dest.id, "filename": name, "state": "success",
                "started_at": now - timedelta(hours=5 - i),
                "finished_at": now - timedelta(hours=5 - i),
            })
        self.dest._prune(transport)
        left = {n for n, _ in transport.list()}
        self.assertEqual(len(left), 3, "keep_count=3 must leave three files")
        self.assertIn("old-4.zip", left, "the newest backup must never be pruned")

    def test_retention_never_deletes_the_only_backup(self):
        Run = self.env["mdx.backup.run"]
        transport = backup_transport.get_transport(self.dest)
        self.dest.write({"keep_count": 0, "keep_days": 1})
        transport.put("ancient.zip", b"x")
        Run.create({
            "destination_id": self.dest.id, "filename": "ancient.zip", "state": "success",
            "started_at": fields.Datetime.now() - timedelta(days=400),
            "finished_at": fields.Datetime.now() - timedelta(days=400),
        })
        self.dest._prune(transport)
        self.assertIn("ancient.zip", [n for n, _ in transport.list()],
                      "a lone backup, however old, is still the only backup there is")

    # ------------------------------------------------------------------
    def test_stale_detection(self):
        self.dest.stale_after_hours = 24
        self.env["mdx.backup.run"].create({
            "destination_id": self.dest.id, "filename": "s.zip", "state": "success",
            "started_at": fields.Datetime.now() - timedelta(hours=48),
            "finished_at": fields.Datetime.now() - timedelta(hours=48),
        })
        self.dest.invalidate_recordset()
        self.assertTrue(self.dest.is_stale, "48h old against a 24h window is stale")

        self.env["mdx.backup.run"].create({
            "destination_id": self.dest.id, "filename": "f.zip", "state": "success",
            "started_at": fields.Datetime.now(), "finished_at": fields.Datetime.now(),
        })
        self.dest.invalidate_recordset()
        self.assertFalse(self.dest.is_stale)

    def test_a_failed_run_does_not_count_as_a_backup(self):
        self.dest.stale_after_hours = 24
        self.env["mdx.backup.run"].create({
            "destination_id": self.dest.id, "filename": "boom.zip", "state": "failed",
            "started_at": fields.Datetime.now(), "finished_at": fields.Datetime.now(),
        })
        self.dest.invalidate_recordset()
        self.assertTrue(self.dest.is_stale,
                        "a run that failed must not reset the staleness clock")

    # ------------------------------------------------------------------
    def test_password_field_is_registered_for_encryption(self):
        """The post-init hook should have asked the vault to protect it."""
        self.env["mdx.backup.destination"]._register_encrypted_fields()
        protected = self.env["mdx.vault.field"].protected_map()
        self.assertIn("password",
                      protected.get("mdx.backup.destination", set()),
                      "the stored FTP/SFTP password must be encrypted at rest")

    def test_password_is_ciphertext_in_the_column(self):
        from odoo.tools import SQL
        from odoo.addons.mdx_credential_vault.models import vault_crypto
        self.env["mdx.backup.destination"]._register_encrypted_fields()
        dest = self.env["mdx.backup.destination"].create({
            "name": "FTP probe", "destination_type": "ftp", "host": "ftp.example.com",
            "username": "u", "password": "SuperSecret1", "db_name": self.env.cr.dbname,
        })
        self.env.flush_all()
        rows = self.env.execute_query(SQL(
            "SELECT %s FROM %s WHERE id = %s",
            SQL.identifier("password"), SQL.identifier("mdx_backup_destination"), dest.id))
        stored = rows[0][0]
        self.assertTrue(vault_crypto.is_encrypted(stored),
                        "a database dump must not reveal the backup destination's password")
        dest.invalidate_recordset()
        self.assertEqual(dest.password, "SuperSecret1",
                         "the transport still has to be able to log in")

    # ------------------------------------------------------------------
    def test_end_to_end_backup_is_verified(self):
        """Dump this database for real, upload, read back, verify."""
        run = self.dest._run_backup()
        self.assertEqual(run.state, "success", run.message)
        self.assertTrue(run.sha256)
        self.assertGreater(run.size_bytes, 0)
        self.assertIn(run.filename, [n for n, _ in
                                     backup_transport.get_transport(self.dest).list()])
        self.assertIn("verified", (run.message or "").lower())
