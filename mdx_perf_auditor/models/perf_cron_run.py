import logging
import time

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PerfCronRun(models.Model):
    _name = "perf.cron.run"
    _description = "Scheduled Action Execution Sample"
    _order = "date desc, id desc"
    _log_access = False

    date = fields.Datetime(default=fields.Datetime.now, index=True)
    cron_id = fields.Many2one("ir.cron", ondelete="cascade", index=True)
    cron_name = fields.Char()
    duration = fields.Float("Duration (s)", digits=(9, 3), aggregator="avg")
    ok = fields.Boolean(default=True)
    error = fields.Char()

    @api.autovacuum
    def _gc_samples(self):
        keep = int(self.env["ir.config_parameter"].sudo().get_param(
            "mdx_perf_auditor.cron_sample_retention_days", "60"
        ))
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=keep)
        stale = self.search([("date", "<", cutoff)], limit=5000)
        stale.unlink()
        return len(stale), len(stale) == 5000


class IrCron(models.Model):
    _inherit = "ir.cron"

    def _callback(self, cron_name, server_action_id):
        start = time.monotonic()
        res = super()._callback(cron_name, server_action_id)
        # super() has committed on success; record a timing sample in the fresh txn.
        try:
            if self.env["ir.config_parameter"].sudo().get_param(
                "mdx_perf_auditor.record_cron_runs", "1"
            ) not in ("0", "False", "false", ""):
                self.env["perf.cron.run"].sudo().create({
                    "cron_id": self.id,
                    "cron_name": cron_name,
                    "duration": round(time.monotonic() - start, 3),
                    "ok": True,
                })
        except Exception:
            _logger.exception("mdx_perf_auditor: could not record cron timing")
        return res
