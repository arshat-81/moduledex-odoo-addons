"""One backup attempt, and what became of it."""

from odoo import _, api, fields, models


class MdxBackupRun(models.Model):
    _name = "mdx.backup.run"
    _description = "Backup Run"
    _order = "started_at desc, id desc"
    _rec_name = "filename"

    destination_id = fields.Many2one(
        "mdx.backup.destination", string="Destination", required=True,
        ondelete="cascade", index=True)
    filename = fields.Char(readonly=True)
    started_at = fields.Datetime(readonly=True, index=True)
    finished_at = fields.Datetime(readonly=True)
    duration_s = fields.Float(string="Seconds", compute="_compute_duration", store=True)
    size_bytes = fields.Integer(string="Size (bytes)", readonly=True)
    size_human = fields.Char(string="Size", compute="_compute_size_human")
    sha256 = fields.Char(string="SHA-256", readonly=True)
    state = fields.Selection(
        [("success", "Verified"), ("unverified", "Unverified"), ("failed", "Failed")],
        required=True, readonly=True, index=True,
        help="Verified means the file was read back from the destination and matched. "
             "Unverified means it was uploaded but could not be confirmed - treat it as "
             "a backup you do not have until you check it yourself.",
    )
    message = fields.Text(readonly=True)
    pruned = fields.Boolean(readonly=True, help="Deleted from the destination by retention.")

    @api.depends("started_at", "finished_at")
    def _compute_duration(self):
        for run in self:
            if run.started_at and run.finished_at:
                run.duration_s = (run.finished_at - run.started_at).total_seconds()
            else:
                run.duration_s = 0.0

    def _compute_size_human(self):
        for run in self:
            size = run.size_bytes or 0
            for unit in ("B", "KB", "MB", "GB"):
                if size < 1024 or unit == "GB":
                    run.size_human = "%.1f %s" % (size, unit) if unit != "B" else "%d B" % size
                    break
                size /= 1024.0
