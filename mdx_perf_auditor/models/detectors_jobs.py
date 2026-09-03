"""Group D — Background jobs. Scheduled actions, the mail queue, and the job
queue if OCA queue_job is installed. Duration data comes from the ir.cron
timing hook in perf_cron_run.py."""

from odoo import _, models
from odoo.tools import SQL

_INTERVAL_SECONDS = {
    "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800, "months": 2592000,
}


class PerfAuditRunJobs(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "D2", "group": "jobs", "method": "_detect_d2_overrun",
             "title": _("Scheduled action overruns its interval"), "severity": "warning"},
            {"code": "D3", "group": "jobs", "method": "_detect_d3_failing_crons",
             "title": _("Scheduled action failing or auto-disabled"), "severity": "critical"},
            {"code": "D5", "group": "jobs", "method": "_detect_d5_mail_queue",
             "title": _("Outbound mail queue backing up"), "severity": "warning"},
            {"code": "D6", "group": "jobs", "method": "_detect_d6_queue_job",
             "title": _("Job queue backlog"), "severity": "warning"},
        ]

    # ------------------------------------------------------------------
    def _detect_d2_overrun(self):
        samples = self._threshold("d2_min_samples", 3)
        rows = self._pg(SQL("""
            SELECT cron_id, avg(duration) AS avg_s, max(duration) AS max_s, count(*) AS n
              FROM perf_cron_run
             WHERE ok = true AND date > now() - interval '14 days'
             GROUP BY cron_id
            HAVING count(*) >= %s
        """, samples))
        out = []
        for r in rows:
            cron = self.env["ir.cron"].sudo().browse(r["cron_id"])
            if not cron.exists():
                continue
            interval_s = (cron.interval_number or 1) * _INTERVAL_SECONDS.get(cron.interval_type, 3600)
            if r["avg_s"] < interval_s * 0.8:
                continue
            out.append({
                "target_kind": "cron",
                "target_ref": cron.cron_name or cron.name,
                "severity": "critical" if r["avg_s"] >= interval_s else "warning",
                "metric": _("avg %.1fs / max %.1fs, runs every %ss") % (
                    r["avg_s"], r["max_s"], int(interval_s)),
                "threshold": _("run time approaching or exceeding the schedule interval"),
                "recommendation": _(
                    "This job cannot reliably finish before it is due again. Split the work, widen "
                    "the interval, or make it batch with _commit_progress and reschedule ASAP."),
                "fix_kind": "none",
            })
        return out

    def _detect_d3_failing_crons(self):
        crons = self.env["ir.cron"].sudo().with_context(active_test=False).search([
            "|", ("failure_count", ">", 0),
            "&", ("active", "=", False), ("first_failure_date", "!=", False),
        ])
        out = []
        for c in crons:
            disabled = not c.active
            out.append({
                "target_kind": "cron",
                "target_ref": c.cron_name or c.name,
                "severity": "critical" if disabled else "warning",
                "metric": (_("auto-disabled after %d failures (first %s)")
                           if disabled else _("%d consecutive failures since %s")) % (
                    c.failure_count or 5,
                    c.first_failure_date and c.first_failure_date.date().isoformat() or "?"),
                "threshold": _("a scheduled action should not be failing"),
                "recommendation": _(
                    "Odoo disables a cron after 5 failures and does not notify anyone. Open the "
                    "action, check the server log for the traceback, fix the cause, then re-enable "
                    "it. Whatever this job does has not run since it stopped."),
                "fix_kind": "none",
            })
        return out

    def _detect_d5_mail_queue(self):
        limit = self._threshold("d5_queue", 200)
        Mail = self.env["mail.mail"].sudo()
        pending = Mail.search_count([("state", "=", "outgoing")])
        failed = Mail.search_count([("state", "=", "exception")])
        if pending < limit and failed < limit:
            return []
        oldest = Mail.search([("state", "=", "outgoing")], order="create_date asc", limit=1)
        return [{
            "target_kind": "other",
            "target_ref": "mail.mail",
            "severity": "critical" if pending >= limit * 5 else "warning",
            "metric": _("%d queued, %d failed; oldest queued %s") % (
                pending, failed,
                oldest.create_date and oldest.create_date.date().isoformat() or "-"),
            "threshold": _("more than %d messages stuck in the queue") % int(limit),
            "recommendation": _(
                "Either the outgoing mail server is rejecting mail or the 'Email Queue Manager' "
                "cron is not running. Check the mail server credentials and the cron."),
            "fix_kind": "none",
        }]

    def _detect_d6_queue_job(self):
        if "queue.job" not in self.env:
            return []
        limit = self._threshold("d6_backlog", 500)
        Job = self.env["queue.job"].sudo()
        pending = Job.search_count([("state", "in", ("pending", "enqueued", "started"))])
        failed = Job.search_count([("state", "=", "failed")])
        if pending < limit and failed < 50:
            return []
        return [{
            "target_kind": "other",
            "target_ref": "queue.job",
            "metric": _("%d pending, %d failed") % (pending, failed),
            "threshold": _("job backlog over %d, or failed jobs present") % int(limit),
            "recommendation": _(
                "The jobrunner is behind or jobs are erroring. Check the jobrunner process, channel "
                "capacity, and the failed jobs' tracebacks."),
            "fix_kind": "none",
        }]
