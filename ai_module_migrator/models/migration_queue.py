import json
import logging
import time
import traceback
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)

# A task still "running" after this long was almost certainly killed with its
# worker (SIGKILL, container restart, OOM, or the cron watchdog in
# ThreadedServer.process_limit). Nothing will come back to finish it, so the
# cron returns it to the pending pool.
#
# Floor for the stale window; the real value is derived per-database by
# _stale_minutes() from the configured provider timeout, because "how long can
# a task legitimately take" is entirely a function of that setting.
STALE_MINUTES = 30

# Worst-case provider calls a single file task can make: the first attempt,
# plus the forced re-asks, the validation repairs, and the legacy-marker fix.
MAX_PROVIDER_CALLS_PER_TASK = 6

# How long a barrier task waits between checks for its siblings to finish.
BARRIER_RETRY_SECONDS = 30

# Hard ceiling on barrier waiting, so a task can never sit pending forever.
BARRIER_MAX_WAIT_HOURS = 24


class AiModuleMigrationQueue(models.Model):
    """A unit of deferred work for the migrator, executed by ir.cron.

    This replaces the module's former dependency on OCA queue_job. queue_job's
    runner only starts when the module is listed in ``server_wide_modules`` in
    odoo.conf, because it monkey-patches Odoo's server classes at import time —
    which is impossible on Odoo.sh and most SaaS hosting, and is an easy step
    for a self-hosted admin to miss (the buttons then fail with "Install and
    load the queue_job module"). A cron-driven queue needs no server
    configuration at all, which is what lets this module stay self-contained.

    Two things here that a generic queue does not need, both driven by what
    code migration actually does:

    * A **barrier** task (``barrier_run_id``): the "build the migrated zip"
      step must run only after every per-file task of the same run has
      finished. queue_job expressed that with ``group(...).on_done(...)``;
      here the finalize task simply defers itself while siblings are still
      active. Failed siblings count as finished, so one dead file task can
      never strand the finalize step forever.
    * A **commit after every task**: one task is one AI provider call costing
      real money and quota. Letting a whole cron batch share a transaction
      would redo completed provider calls whenever any later task killed the
      worker.
    """

    _name = "ai.module.migration.queue"
    _description = "Odoo Module Upgrade AI Background Task"
    _order = "priority asc, id asc"
    _rec_name = "name"

    name = fields.Char(required=True, readonly=True)
    migration_job_id = fields.Many2one(
        "ai.module.migration.job",
        string="Migration Job",
        ondelete="cascade",
        index=True,
        readonly=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Queued By",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
        help="The task runs as this user, so it sees exactly the access rights "
             "the person who queued it had.",
    )
    model_name = fields.Char(required=True, readonly=True)
    res_id = fields.Integer(required=True, readonly=True)
    method = fields.Char(required=True, readonly=True)
    payload = fields.Text(readonly=True, help="JSON keyword arguments passed to the method.")

    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        default="pending",
        required=True,
        index=True,
        readonly=True,
    )
    priority = fields.Integer(default=10, readonly=True)
    run_after = fields.Datetime(
        readonly=True,
        index=True,
        help="Not picked up before this time (used for retry backoff and barrier waiting).",
    )
    run_id = fields.Char(readonly=True, index=True, help="Groups every task queued by one action.")
    barrier_run_id = fields.Char(
        readonly=True,
        index=True,
        help="If set, this task waits until no task with that run_id is pending or running.",
    )

    attempts = fields.Integer(default=0, readonly=True)
    max_attempts = fields.Integer(default=3, readonly=True)
    deferred_count = fields.Integer(default=0, readonly=True)
    error = fields.Text(readonly=True)
    started_at = fields.Datetime(readonly=True)
    finished_at = fields.Datetime(readonly=True)

    _state_idx = models.Index("(state, run_after, priority, id)")

    # ── queueing ──────────────────────────────────────────────────────────

    @api.model
    def enqueue(self, record, method, description, priority=10, run_id=False,
                barrier_run_id=False, max_attempts=None, **kwargs):
        """Queue `record.method(**kwargs)` for later execution by the cron."""
        if not record:
            raise UserError(_("Cannot queue a task without a target record."))
        record.ensure_one()
        migration_job = record if record._name == "ai.module.migration.job" else getattr(record, "job_id", False)
        return self.sudo().create({
            "name": description,
            "migration_job_id": migration_job.id if migration_job else False,
            "user_id": self.env.user.id,
            "model_name": record._name,
            "res_id": record.id,
            "method": method,
            "payload": json.dumps(kwargs, default=str),
            "priority": priority,
            "run_id": run_id or False,
            "barrier_run_id": barrier_run_id or False,
            "max_attempts": self._default_max_attempts() if max_attempts is None else max_attempts,
        })

    @api.model
    def _default_max_attempts(self):
        raw = self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.job_max_attempts")
        try:
            return max(1, min(10, int(raw or 3)))
        except (TypeError, ValueError):
            return 3

    @api.model
    def _default_batch_size(self):
        raw = self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.job_batch_size")
        try:
            return max(1, min(100, int(raw or 5)))
        except (TypeError, ValueError):
            return 5

    # ── execution ─────────────────────────────────────────────────────────

    @api.model
    def _watchdog_seconds(self):
        """The real-time limit Odoo will enforce on this cron thread, or 0 if none.

        Mirrors ThreadedServer.process_limit(): cron threads fall back to
        limit_time_real unless limit_time_real_cron is set above zero.
        """
        try:
            cron_limit = int(config.get("limit_time_real_cron") or 0)
        except (TypeError, ValueError):
            cron_limit = 0
        if cron_limit > 0:
            return cron_limit
        try:
            return max(0, int(config.get("limit_time_real") or 0))
        except (TypeError, ValueError):
            return 0

    @api.model
    def _stale_minutes(self):
        """How long a 'running' task may sit before it is presumed dead.

        Derived from the provider timeout rather than fixed, because that is
        what actually bounds a task: worst case it makes
        MAX_PROVIDER_CALLS_PER_TASK calls, each allowed request_timeout
        seconds. A flat constant is wrong in both directions — too short and a
        live task is reclaimed and its AI calls paid for twice, too long and a
        task killed by a restart blocks its run for hours.
        """
        try:
            timeout = int(self.env["ir.config_parameter"].sudo().get_param(
                "ai_module_migrator.request_timeout") or 600)
        except (TypeError, ValueError):
            timeout = 600
        worst_case = (timeout * MAX_PROVIDER_CALLS_PER_TASK) / 60.0
        return int(max(STALE_MINUTES, worst_case + 10))

    @api.model
    def cron_run_jobs(self, limit=None):
        """Run pending tasks one at a time until the batch or time budget is spent.

        Claims ONE task per iteration rather than a batch up front. Claiming a
        batch and then stopping early (budget spent, worker killed) would leave
        the unrun remainder marked 'running' with nothing executing them, stalled
        until the stale sweep — so the queue would look busy while doing nothing.

        Runs as sudo so the queue keeps working regardless of which user the
        scheduled action is configured to run as; each task is still executed as
        the user who queued it (see _execute).
        """
        queue = self.sudo()
        queue._reclaim_stale()

        batch = limit or queue._default_batch_size()
        # Stop starting new tasks once we are half way to the watchdog. We
        # cannot make an individual provider call fit — only the admin raising
        # limit_time_real_cron can do that — but we can avoid compounding
        # several tasks into one thread and turning a survivable run into a
        # server reload.
        watchdog = queue._watchdog_seconds()
        budget = watchdog * 0.5 if watchdog else 0
        started = time.monotonic()

        done = 0
        while done < batch:
            if budget and done and (time.monotonic() - started) > budget:
                _logger.info(
                    "AI Migrator queue: stopping after %s task(s), %.0fs of a %.0fs budget used; "
                    "the rest stay pending for the next run.",
                    done, time.monotonic() - started, budget,
                )
                break
            task = queue._claim(1)
            if not task:
                break
            task._execute()
            # One task is one paid AI call. Persist its outcome now so a later
            # task that kills the worker cannot roll back work already done.
            queue.env.cr.commit()
            done += 1
        if done:
            _logger.info("AI Migrator queue: ran %s task(s)", done)
        return done

    @api.model
    def _claim(self, limit):
        """Atomically mark a batch as running.

        SKIP LOCKED means two cron threads never take the same task and never
        block on each other either.
        """
        now = fields.Datetime.now()
        # The claim below is raw SQL and bypasses the ORM cache, so pending
        # writes must reach the database first — notably the rows
        # _reclaim_stale() just returned to 'pending'.
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT id FROM ai_module_migration_queue
             WHERE state = 'pending'
               AND (run_after IS NULL OR run_after <= %s)
             ORDER BY priority ASC, id ASC
             LIMIT %s
               FOR UPDATE SKIP LOCKED
            """,
            (now, limit),
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        if not ids:
            return self.browse()
        tasks = self.browse(ids)
        tasks.write({"state": "running", "started_at": now})
        # Commit the claim immediately: if this worker dies mid-task the rows
        # stay 'running' and are recovered by _reclaim_stale() after
        # STALE_MINUTES, rather than being picked up again straight away and
        # paying for the same AI call twice.
        self.env.cr.commit()
        return tasks

    @api.model
    def _reclaim_stale(self):
        """Return tasks orphaned by a killed worker to the pending pool."""
        cutoff = fields.Datetime.now() - timedelta(minutes=self._stale_minutes())
        stale = self.search([("state", "=", "running"), ("started_at", "<", cutoff)])
        if stale:
            _logger.warning("AI Migrator queue: reclaiming %s stale task(s)", len(stale))
            stale.write({"state": "pending", "started_at": False})

    def _barrier_is_blocked(self):
        """True while any sibling task of the barrier run is still unfinished.

        Only 'pending' and 'running' count. A sibling that ended 'failed' or
        'cancelled' is finished as far as the barrier is concerned — otherwise
        one dead file task would keep the finalize step waiting forever.
        """
        self.ensure_one()
        if not self.barrier_run_id:
            return False
        return bool(self.sudo().search_count([
            ("id", "!=", self.id),
            ("run_id", "=", self.barrier_run_id),
            ("state", "in", ("pending", "running")),
        ]))

    def _defer(self):
        """Put a barrier task back to pending without consuming an attempt."""
        self.ensure_one()
        self.write({
            "state": "pending",
            "started_at": False,
            "run_after": fields.Datetime.now() + timedelta(seconds=BARRIER_RETRY_SECONDS),
            "deferred_count": self.deferred_count + 1,
        })

    def _execute(self):
        """Run one task. Never raises — failure is recorded on the task."""
        self.ensure_one()

        if self._barrier_is_blocked():
            waited = fields.Datetime.now() - (self.create_date or fields.Datetime.now())
            if waited > timedelta(hours=BARRIER_MAX_WAIT_HOURS):
                self.write({
                    "state": "failed",
                    "error": _(
                        "Waited over %s hours for the other tasks of this run to finish."
                    ) % BARRIER_MAX_WAIT_HOURS,
                    "finished_at": fields.Datetime.now(),
                })
                return False
            self._defer()
            return False

        try:
            kwargs = json.loads(self.payload or "{}")
        except ValueError:
            kwargs = {}

        # A savepoint, not a bare try/except: a Postgres error aborts the whole
        # transaction, so without this one bad task would poison everything
        # already written in this cron run.
        try:
            with self.env.cr.savepoint():
                record = self.env[self.model_name].with_user(self.user_id).browse(self.res_id).exists()
                if not record:
                    raise UserError(_("Target record no longer exists."))
                getattr(record, self.method)(**kwargs)
        except Exception as exc:  # noqa: BLE001 - the queue must survive any task
            self._record_failure(exc)
            return False

        self.write({
            "state": "done",
            "error": False,
            "attempts": self.attempts + 1,
            "finished_at": fields.Datetime.now(),
        })
        return True

    def _record_failure(self, exc):
        attempts = self.attempts + 1
        vals = {
            "attempts": attempts,
            "error": "%s\n\n%s" % (exc, traceback.format_exc()),
            "finished_at": fields.Datetime.now(),
        }
        if attempts >= self.max_attempts:
            vals["state"] = "failed"
        else:
            # exponential backoff: 1, 2, 4 ... minutes
            vals["state"] = "pending"
            vals["started_at"] = False
            vals["run_after"] = fields.Datetime.now() + timedelta(minutes=2 ** (attempts - 1))
        self.write(vals)
        _logger.warning(
            "AI Migrator task %s (%s) failed on attempt %s/%s: %s",
            self.id, self.method, attempts, self.max_attempts, exc,
        )

    # ── user actions ──────────────────────────────────────────────────────

    def action_requeue(self):
        """Put failed tasks back in the pending pool with a fresh attempt count."""
        self.filtered(lambda task: task.state in ("failed", "cancelled")).write({
            "state": "pending", "attempts": 0, "error": False,
            "run_after": False, "started_at": False,
        })
        return True

    def action_cancel(self):
        self.filtered(lambda task: task.state in ("pending", "failed")).write({"state": "cancelled"})
        return True

    def action_run_now(self):
        """Run selected tasks immediately, ignoring the cron schedule."""
        for task in self.filtered(lambda t: t.state in ("pending", "failed")):
            task.write({"state": "running", "started_at": fields.Datetime.now()})
            task._execute()
        return True

    @api.model
    def cron_vacuum(self, days=7):
        """Drop old finished tasks so the table does not grow without bound."""
        cutoff = fields.Datetime.now() - timedelta(days=days)
        old = self.search([
            ("state", "in", ("done", "cancelled")),
            ("finished_at", "<", cutoff),
        ])
        count = len(old)
        old.unlink()
        return count
