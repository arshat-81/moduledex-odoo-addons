import json
import logging
import traceback
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# A job that has been "running" longer than this was almost certainly killed
# with its worker (SIGKILL, container restart, OOM). Nothing will ever come
# back to finish it, so the cron reclaims it.
STALE_MINUTES = 30


class BigcommerceJob(models.Model):
    """A unit of deferred work, executed by ir.cron.

    Deliberately NOT built on OCA queue_job: that module's runner only starts
    when it is listed in ``server_wide_modules`` in odoo.conf, because it
    monkey-patches Odoo's server classes at import time. That is impossible on
    Odoo.sh and most SaaS hosting. A cron-driven queue needs no configuration
    at all, which is what lets this module stay self-contained.
    """

    _name = "bigcommerce.job"
    _description = "BigCommerce Deferred Job"
    _order = "priority asc, id asc"
    _rec_name = "name"

    name = fields.Char(required=True, readonly=True)
    config_id = fields.Many2one("bigcommerce.config", required=True,
                                ondelete="cascade", readonly=True, index=True)
    model_name = fields.Char(required=True, readonly=True)
    res_id = fields.Integer(required=True, readonly=True)
    method = fields.Char(required=True, readonly=True)
    payload = fields.Text(readonly=True, help="JSON keyword arguments passed to the method.")

    state = fields.Selection(
        [("pending", "Pending"), ("running", "Running"),
         ("done", "Done"), ("failed", "Failed"), ("cancelled", "Cancelled")],
        default="pending", required=True, index=True, readonly=True)
    priority = fields.Integer(default=10, readonly=True)
    run_after = fields.Datetime(readonly=True, index=True,
                                help="Not picked up before this time (used for retry backoff).")

    attempts = fields.Integer(default=0, readonly=True)
    max_attempts = fields.Integer(default=3, readonly=True)
    error = fields.Text(readonly=True)
    run_id = fields.Char(readonly=True, index=True,
                         help="Groups every job queued by one wizard run.")
    started_at = fields.Datetime(readonly=True)
    finished_at = fields.Datetime(readonly=True)

    _state_idx = models.Index("(state, run_after, priority, id)")

    # ── queueing ──────────────────────────────────────────────────────────

    @api.model
    def enqueue(self, record, method, description, config=None, priority=10, **kwargs):
        """Queue `record.method(**kwargs)` for later execution.

        `config` is passed explicitly by dispatch() - deriving it from
        record.config_id would assume every queueable model carries that
        field, which is not true (a config can queue work against itself).
        """
        if not record:
            raise UserError(_("Cannot queue a job without a target record."))
        record.ensure_one()
        # run_id stays INSIDE kwargs so it reaches the target method in the
        # payload; it is only read here to tag the job row. Taking it as a
        # separate parameter would collide with the copy in kwargs.
        run_id = kwargs.get("run_id")
        config = config or getattr(record, "config_id", False) or record
        if config._name != "bigcommerce.config":
            raise UserError(_("Cannot resolve the store for this job."))
        return self.create({
            "name": description,
            "config_id": config.id,
            "model_name": record._name,
            "res_id": record.id,
            "method": method,
            "payload": json.dumps(kwargs, default=str),
            "run_id": run_id,
            "priority": priority,
            "max_attempts": config.job_max_attempts or 3,
        })

    # ── execution ─────────────────────────────────────────────────────────

    @api.model
    def cron_run_jobs(self, limit=None):
        """Claim a batch of pending jobs and run them, one savepoint each."""
        self._reclaim_stale()
        limit = limit or 50
        jobs = self._claim(limit)
        if not jobs:
            return 0
        _logger.info("BigCommerce queue: running %s job(s)", len(jobs))
        for job in jobs:
            job._execute()
        return len(jobs)

    @api.model
    def _claim(self, limit):
        """Atomically mark a batch as running.

        SKIP LOCKED means two cron workers never take the same job, and never
        block on each other either.
        """
        now = fields.Datetime.now()
        # Odoo flushes ORM writes lazily, but the claim below is raw SQL and so
        # bypasses the cache entirely. Without this flush it reads stale rows -
        # notably the ones _reclaim_stale() just returned to 'pending', and any
        # job enqueued earlier in this same transaction.
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT id FROM bigcommerce_job
             WHERE state = 'pending'
               AND (run_after IS NULL OR run_after <= %s)
             ORDER BY priority ASC, id ASC
             LIMIT %s
               FOR UPDATE SKIP LOCKED
            """, (now, limit))
        ids = [r[0] for r in self.env.cr.fetchall()]
        if not ids:
            return self.browse()
        jobs = self.browse(ids)
        jobs.write({"state": "running", "started_at": now})
        return jobs

    @api.model
    def _reclaim_stale(self):
        """Return jobs orphaned by a killed worker to the pending pool."""
        cutoff = fields.Datetime.now() - timedelta(minutes=STALE_MINUTES)
        stale = self.search([("state", "=", "running"), ("started_at", "<", cutoff)])
        if stale:
            _logger.warning("BigCommerce queue: reclaiming %s stale job(s)", len(stale))
            stale.write({"state": "pending", "started_at": False})

    def _execute(self):
        """Run one job. Never raises - failure is recorded on the job."""
        self.ensure_one()
        try:
            kwargs = json.loads(self.payload or "{}")
        except ValueError:
            kwargs = {}

        # A savepoint, not a bare try/except: a Postgres error aborts the whole
        # transaction, so without this one bad job would poison the rest of the
        # batch and roll back the jobs that already succeeded.
        try:
            with self.env.cr.savepoint():
                record = self.env[self.model_name].browse(self.res_id).exists()
                if not record:
                    raise UserError(_("Target record no longer exists."))
                getattr(record, self.method)(**kwargs)
        except Exception as exc:  # noqa: BLE001 - the queue must survive any job
            self._record_failure(exc)
            return False

        self.write({"state": "done", "error": False,
                    "attempts": self.attempts + 1,
                    "finished_at": fields.Datetime.now()})
        return True

    def _record_failure(self, exc):
        attempts = self.attempts + 1
        exhausted = attempts >= self.max_attempts
        vals = {
            "attempts": attempts,
            "error": "%s\n\n%s" % (exc, traceback.format_exc()),
            "finished_at": fields.Datetime.now(),
        }
        if exhausted:
            vals["state"] = "failed"
        else:
            # exponential backoff: 1, 2, 4 ... minutes
            vals["state"] = "pending"
            vals["run_after"] = fields.Datetime.now() + timedelta(minutes=2 ** (attempts - 1))
        self.write(vals)
        _logger.warning("BigCommerce job %s failed (attempt %s/%s): %s",
                        self.id, attempts, self.max_attempts, exc)
        self.env["bigcommerce.update.log"].log(
            self.config_id, "job:%s" % self.method, status="error", message=str(exc))

    # ── user actions ──────────────────────────────────────────────────────

    def action_requeue(self):
        """Put failed jobs back in the pending pool with a fresh attempt count."""
        stuck = self.filtered(lambda j: j.state in ("failed", "cancelled"))
        stuck.write({"state": "pending", "attempts": 0, "error": False, "run_after": False})
        return True

    def action_cancel(self):
        self.filtered(lambda j: j.state in ("pending", "failed")).write({"state": "cancelled"})
        return True

    def action_run_now(self):
        """Run selected jobs immediately, ignoring the cron schedule."""
        for job in self.filtered(lambda j: j.state in ("pending", "failed")):
            job.write({"state": "running", "started_at": fields.Datetime.now()})
            job._execute()
        return True

    @api.model
    def cron_vacuum(self, days=7):
        """Drop old finished jobs so the table does not grow without bound."""
        cutoff = fields.Datetime.now() - timedelta(days=days)
        old = self.search([("state", "=", "done"), ("finished_at", "<", cutoff)])
        count = len(old)
        old.unlink()
        return count
