"""A place backups are sent, and the schedule that sends them."""

import hashlib
import io
import logging
import zipfile
from datetime import timedelta

import odoo
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.service import db as db_service

from . import backup_transport

_logger = logging.getLogger(__name__)

# What a valid Odoo zip dump must contain. Checked on the copy read back from
# the destination, which is the difference between "the upload returned 200"
# and "there is a restorable backup over there".
REQUIRED_MEMBERS = ("dump.sql", "manifest.json")


class MdxBackupDestination(models.Model):
    _name = "mdx.backup.destination"
    _description = "Backup Destination"
    _order = "sequence, name"
    _inherit = ["mail.thread"]

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, tracking=True)
    destination_type = fields.Selection(
        [("local", "Folder on this server"), ("ftp", "FTP / FTPS"), ("sftp", "SFTP")],
        default="local", required=True, tracking=True,
    )
    db_name = fields.Char(
        string="Database", required=True, tracking=True,
        default=lambda self: self.env.cr.dbname,
        help="The database to dump. Defaults to the one you are using.",
    )
    with_filestore = fields.Boolean(
        string="Include Filestore", default=True,
        help="Attachments and images. Without them a restore comes back with missing "
             "documents, so this is on unless you deliberately back the filestore up "
             "some other way.",
    )

    # -- connection ---------------------------------------------------
    local_path = fields.Char(string="Folder")
    host = fields.Char()
    port = fields.Integer()
    username = fields.Char()
    # Encrypted at rest by mdx_credential_vault, which this module registers
    # the field with on install.
    password = fields.Char()
    remote_path = fields.Char(string="Remote Folder")
    use_tls = fields.Boolean(string="Use TLS (FTPS)", default=True)
    timeout = fields.Integer(default=60, help="Seconds before a transfer is abandoned.")

    # -- schedule -----------------------------------------------------
    interval_hours = fields.Integer(
        string="Every (hours)", default=24, required=True,
        help="A backup is taken when this many hours have passed since the last successful one.",
    )
    next_due = fields.Datetime(compute="_compute_next_due", store=False)

    # -- retention ----------------------------------------------------
    keep_count = fields.Integer(
        string="Keep Last", default=7,
        help="Delete older backups beyond this many. 0 keeps everything.",
    )
    keep_days = fields.Integer(
        string="Keep Days", default=0,
        help="Delete backups older than this many days. 0 disables age-based deletion.",
    )

    # -- alerting -----------------------------------------------------
    alert_user_ids = fields.Many2many(
        "res.users", string="Alert",
        help="Notified when a backup fails, and when this destination goes quiet for longer "
             "than the stale threshold below.",
    )
    stale_after_hours = fields.Integer(
        string="Warn After (hours)", default=48,
        help="Raise an alert when there has been no successful backup for this long. This is "
             "the alert that matters: a backup job that silently stopped looks exactly like "
             "one that never ran.",
    )

    # -- state --------------------------------------------------------
    run_ids = fields.One2many("mdx.backup.run", "destination_id", string="History")
    run_count = fields.Integer(compute="_compute_stats")
    last_success = fields.Datetime(compute="_compute_stats", store=True)
    last_status = fields.Selection(
        [("success", "Verified"), ("unverified", "Unverified"), ("failed", "Failed")],
        compute="_compute_stats", store=True,
    )
    last_size = fields.Integer(compute="_compute_stats", string="Last Size (bytes)")
    is_stale = fields.Boolean(compute="_compute_stale", search="_search_stale")

    @api.depends("run_ids.state", "run_ids.finished_at")
    def _compute_stats(self):
        for dest in self:
            # Ordered by when the run actually finished, not by id. Ids normally
            # track time, but a restored or imported history does not, and
            # "when did this last work" must not be answered by insertion order.
            runs = dest.run_ids.sorted(
                lambda r: (r.finished_at or r.started_at or fields.Datetime.now(), r.id),
                reverse=True)
            dest.run_count = len(runs)
            last = runs[:1]
            dest.last_status = last.state if last else False
            dest.last_size = last.size_bytes if last else 0
            good = runs.filtered(lambda r: r.state == "success")[:1]
            dest.last_success = good.finished_at if good else False

    def _compute_next_due(self):
        for dest in self:
            if dest.last_success and dest.interval_hours:
                dest.next_due = dest.last_success + timedelta(hours=dest.interval_hours)
            else:
                dest.next_due = fields.Datetime.now()

    @api.depends("last_success", "stale_after_hours")
    def _compute_stale(self):
        now = fields.Datetime.now()
        for dest in self:
            if not dest.stale_after_hours:
                dest.is_stale = False
            elif not dest.last_success:
                dest.is_stale = bool(dest.run_count)
            else:
                dest.is_stale = (now - dest.last_success) > timedelta(hours=dest.stale_after_hours)

    def _search_stale(self, operator, value):
        stale = self.search([]).filtered("is_stale")
        if operator in ("=", "==") and value:
            return [("id", "in", stale.ids)]
        return [("id", "not in", stale.ids)]

    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        backup_transport.get_transport(self).check()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Connection"),
                       "message": _("%s is reachable and writable.", self.name),
                       "type": "success", "sticky": False},
        }

    def action_backup_now(self):
        for dest in self:
            dest._run_backup()
        return True

    def action_view_runs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "res_model": "mdx.backup.run",
            "name": _("Backups of %s", self.name), "view_mode": "list,form",
            "domain": [("destination_id", "=", self.id)],
        }

    # ------------------------------------------------------------------
    def _filename(self):
        stamp = fields.Datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return "%s_%s.zip" % (self.db_name, stamp)

    def _run_backup(self):
        """Dump, upload, read back, verify, prune. Never raises."""
        self.ensure_one()
        Run = self.env["mdx.backup.run"].sudo()
        run = Run.create({
            "destination_id": self.id,
            "started_at": fields.Datetime.now(),
            "state": "failed",
            "filename": "",
        })
        filename = self._filename()
        try:
            transport = backup_transport.get_transport(self)
            transport.check()

            stream = io.BytesIO()
            db_service.dump_db(self.db_name, stream, backup_format="zip",
                               with_filestore=self.with_filestore)
            payload = stream.getvalue()
            if not payload:
                raise UserError(_("The dump produced no data."))
            digest = hashlib.sha256(payload).hexdigest()

            transport.put(filename, payload)

            # the point of the module: prove the copy that landed is usable
            verified, detail = self._verify(transport, filename, payload, digest)

            run.write({
                "filename": filename,
                "size_bytes": len(payload),
                "sha256": digest,
                "state": "success" if verified else "unverified",
                "message": detail,
                "finished_at": fields.Datetime.now(),
            })
            if verified:
                self._prune(transport)
        except Exception as error:  # noqa: BLE001 - a backup job must not crash the cron
            _logger.exception("mdx_backup_manager: backup to %s failed", self.name)
            run.write({
                "filename": filename,
                "state": "failed",
                "message": str(error)[:2000],
                "finished_at": fields.Datetime.now(),
            })
            self._alert(_("Backup failed: %s", self.name), str(error)[:500])
        return run

    def _verify(self, transport, filename, payload, digest):
        """Read the file back and confirm it is the archive we sent."""
        try:
            returned = transport.fetch(filename)
        except Exception as error:  # noqa: BLE001
            return False, _("Uploaded, but could not be read back: %s", error)

        if len(returned) != len(payload):
            return False, _("Uploaded %(sent)s bytes but read back %(got)s.",
                            sent=len(payload), got=len(returned))
        if hashlib.sha256(returned).hexdigest() != digest:
            return False, _("The copy at the destination does not match the checksum of what "
                            "was sent. Treat this backup as unusable.")
        try:
            with zipfile.ZipFile(io.BytesIO(returned)) as archive:
                names = set(archive.namelist())
                missing = [m for m in REQUIRED_MEMBERS if m not in names]
                if missing:
                    return False, _("The archive is missing %s.", ", ".join(missing))
                broken = archive.testzip()
                if broken:
                    return False, _("The archive is corrupt at %s.", broken)
        except zipfile.BadZipFile:
            return False, _("What came back is not a readable zip archive.")
        return True, _("Read back and verified: checksum matches and the archive contains "
                       "dump.sql and manifest.json.")

    def _prune(self, transport):
        """Apply retention, oldest first, never touching the newest backup."""
        self.ensure_one()
        if not self.keep_count and not self.keep_days:
            return
        runs = self.env["mdx.backup.run"].sudo().search([
            ("destination_id", "=", self.id), ("state", "in", ("success", "unverified")),
            ("filename", "!=", False),
        ], order="finished_at desc")
        doomed = self.env["mdx.backup.run"].sudo()
        if self.keep_count and len(runs) > self.keep_count:
            doomed |= runs[self.keep_count:]
        if self.keep_days:
            cutoff = fields.Datetime.now() - timedelta(days=self.keep_days)
            doomed |= runs.filtered(lambda r: r.finished_at and r.finished_at < cutoff)
        doomed -= runs[:1]  # never delete the most recent one, whatever the rules say
        for run in doomed:
            try:
                transport.remove(run.filename)
                run.write({"pruned": True})
            except Exception:  # noqa: BLE001
                _logger.warning("mdx_backup_manager: could not delete %s", run.filename)

    def _alert(self, subject, body):
        self.ensure_one()
        if not self.alert_user_ids:
            return
        partners = self.alert_user_ids.mapped("partner_id")
        self.message_notify(
            partner_ids=partners.ids,
            subject=subject,
            body="<p>%s</p>" % body,
        )

    # ------------------------------------------------------------------
    @api.model
    def _cron_run_due(self):
        """Take backups that are due, then complain about the quiet ones."""
        now = fields.Datetime.now()
        for dest in self.search([("active", "=", True)]):
            due = (not dest.last_success or not dest.interval_hours
                   or dest.last_success + timedelta(hours=dest.interval_hours) <= now)
            if due:
                dest._run_backup()
        self._cron_check_stale()
        return True

    @api.model
    def _cron_check_stale(self):
        for dest in self.search([("active", "=", True)]):
            if dest.is_stale and dest.alert_user_ids:
                dest._alert(
                    _("No recent backup: %s", dest.name),
                    _("The last verified backup was %(when)s. This destination is configured to "
                      "warn after %(hours)s hours.",
                      when=dest.last_success or _("never"), hours=dest.stale_after_hours))
        return True

    # ------------------------------------------------------------------
    @api.model
    def _register_encrypted_fields(self):
        """Ask the credential vault to protect this model's password column.

        Called from the post-init hook. Nothing here reimplements encryption -
        the vault owns that, and this module simply declares what needs it.
        """
        Vault = self.env.get("mdx.vault.field")
        if Vault is None:
            return False
        model = self.env["ir.model"]._get(self._name)
        field = self.env["ir.model.fields"].search([
            ("model", "=", self._name), ("name", "=", "password")], limit=1)
        if not model or not field:
            return False
        if Vault.search_count([("model_id", "=", model.id), ("field_id", "=", field.id)]):
            return True
        Vault.create({"model_id": model.id, "field_id": field.id})
        _logger.info("mdx_backup_manager: registered %s.password for encryption", self._name)
        return True
