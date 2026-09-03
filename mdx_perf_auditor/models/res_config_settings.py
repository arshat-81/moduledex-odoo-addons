"""Settings panel. Everything tunable was an ir.config_parameter you had to know
the name of; the ones that actually get tuned now have a field."""

from odoo import api, fields, models

P = "mdx_perf_auditor."


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    perf_alert_user_ids = fields.Many2many(
        "res.users", string="Alert recipients",
        help="Emailed when a scheduled audit turns up a new finding at or above "
             "the severity below. Leave empty to disable alerting.")
    perf_alert_severity = fields.Selection(
        [("critical", "Critical only"), ("warning", "Critical and warnings")],
        string="Alert on", default="critical")

    perf_snapshot_retention_days = fields.Integer(
        string="Keep database snapshots for (days)", default=90)
    perf_cron_sample_retention_days = fields.Integer(
        string="Keep scheduled-action timings for (days)", default=60)
    perf_record_cron_runs = fields.Boolean(
        string="Time scheduled actions", default=True,
        help="Records how long each cron takes, which is what detector D2 uses to "
             "spot a job that cannot finish before it is due again.")

    perf_capture_default_minutes = fields.Integer(
        string="Default capture length (minutes)", default=10)

    # the handful of thresholds worth exposing; the rest stay config parameters
    perf_a1_min_table_bytes = fields.Integer(
        string="Ignore unindexed FKs on tables under (MB)", default=4)
    perf_c3_repeat = fields.Integer(
        string="Flag N+1 above (repeats per request)", default=20)
    perf_g_min_ms = fields.Float(
        string="Explain statements slower than (ms)", default=120.0)
    perf_h1_mb_per_day = fields.Float(
        string="Flag table growth above (MB/day)", default=20.0)

    @api.model
    def get_values(self):
        res = super().get_values()
        p = self.env["ir.config_parameter"].sudo()

        def num(key, default, cast=int):
            raw = p.get_param(P + key)
            try:
                return cast(raw) if raw not in (None, False, "") else default
            except (TypeError, ValueError):
                return default

        ids = [int(i) for i in (p.get_param(P + "alert_user_ids") or "").split(",")
               if i.strip().isdigit()]
        res.update(
            perf_alert_user_ids=[(6, 0, ids)],
            perf_alert_severity=p.get_param(P + "alert_severity") or "critical",
            perf_snapshot_retention_days=num("snapshot_retention_days", 90),
            perf_cron_sample_retention_days=num("cron_sample_retention_days", 60),
            perf_record_cron_runs=(p.get_param(P + "record_cron_runs", "1")
                                   not in ("0", "False", "false", "")),
            perf_capture_default_minutes=num("capture_default_minutes", 10),
            perf_a1_min_table_bytes=num("threshold.a1_min_table_bytes", 4 * 1024 * 1024) // (1024 * 1024),
            perf_c3_repeat=num("threshold.c3_repeat", 20),
            perf_g_min_ms=num("threshold.g_min_ms", 120.0, float),
            perf_h1_mb_per_day=num("threshold.h1_mb_per_day", 20.0, float),
        )
        return res

    def set_values(self):
        super().set_values()
        p = self.env["ir.config_parameter"].sudo()
        p.set_param(P + "alert_user_ids", ",".join(str(i) for i in self.perf_alert_user_ids.ids))
        p.set_param(P + "alert_severity", self.perf_alert_severity or "critical")
        p.set_param(P + "snapshot_retention_days", str(max(1, self.perf_snapshot_retention_days)))
        p.set_param(P + "cron_sample_retention_days", str(max(1, self.perf_cron_sample_retention_days)))
        p.set_param(P + "record_cron_runs", "1" if self.perf_record_cron_runs else "0")
        p.set_param(P + "capture_default_minutes", str(max(1, min(120, self.perf_capture_default_minutes))))
        p.set_param(P + "threshold.a1_min_table_bytes",
                    str(max(0, self.perf_a1_min_table_bytes) * 1024 * 1024))
        p.set_param(P + "threshold.c3_repeat", str(max(2, self.perf_c3_repeat)))
        p.set_param(P + "threshold.g_min_ms", str(max(1.0, self.perf_g_min_ms)))
        p.set_param(P + "threshold.h1_mb_per_day", str(max(0.1, self.perf_h1_mb_per_day)))
