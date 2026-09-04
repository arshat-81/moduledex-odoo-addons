import logging
import re

from markupsafe import Markup

import odoo
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

# Maintenance statements the "Apply safe fix" button is allowed to run.
#
# This matches the WHOLE statement, not just its opening verb. An earlier version
# anchored only the start, which let `ANALYZE; DROP TABLE x` through — psycopg2
# executes stacked statements in one call, so that was arbitrary SQL behind a
# button. Identifiers must be double-quoted and contain nothing but word
# characters, which also makes a semicolon unrepresentable.
_IDENT = r'"[A-Za-z0-9_$]+"'
_SAFE_SQL_RE = re.compile(
    r"""^\s*(
          ANALYZE\s+{ident}
        | VACUUM (\s*\(\s*ANALYZE\s*\))? \s+{ident}
        | CREATE\s+INDEX\s+CONCURRENTLY\s+IF\s+NOT\s+EXISTS\s+{ident}
          \s+ON\s+{ident}\s*\(\s*{ident}(\s*,\s*{ident})*\s*\)
    )\s*$""".format(ident=_IDENT),
    re.IGNORECASE | re.VERBOSE,
)


def is_safe_maintenance_sql(statement):
    """True only for the three statement shapes this module may execute."""
    sql = (statement or "").strip().rstrip(";").strip()
    if ";" in sql or "--" in sql or "/*" in sql:
        return False
    return bool(_SAFE_SQL_RE.match(sql))


FIX_LABELS = {
    "none": "",
    "create_index": "Create index (concurrently)",
    "analyze": "Run ANALYZE",
    "vacuum": "Run VACUUM",
    "drop_index": "Drop index (manual)",
    "config": "Configuration change (manual)",
}


class PerfAuditFinding(models.Model):
    _name = "perf.audit.finding"
    _description = "Performance Audit Finding"
    _inherit = ["mail.thread"]
    _order = "severity_order asc, category asc, detector_code asc"

    name = fields.Char(required=True, tracking=True)
    run_id = fields.Many2one("perf.audit.run", string="Last seen in", ondelete="set null")
    detector_code = fields.Char(required=True, index=True)
    category = fields.Selection(
        [
            ("db", "Database"),
            ("orm", "ORM & model design"),
            ("runtime", "Runtime"),
            ("jobs", "Background jobs"),
            ("frontend", "Assets & frontend"),
            ("config", "Configuration"),
        ],
        required=True,
        index=True,
    )
    severity = fields.Selection(
        [("critical", "Critical"), ("warning", "Warning"), ("info", "Advisory")],
        required=True,
        default="warning",
        tracking=True,
    )
    severity_order = fields.Integer(compute="_compute_severity_order", store=True)
    depth = fields.Selection(
        [("quick", "Quick scan"), ("deep", "Deep analysis")],
        default="quick",
        index=True,
        help="Whether this finding comes from the fast scan or from a deep analysis pass.",
    )
    evidence = fields.Text(help="Query plan extract, sample statement or other raw proof.")
    planner_verdict = fields.Selection(
        [("untested", "Not tested"), ("used", "Planner would use it"),
         ("unused", "Planner would ignore it")],
        default="untested",
        help="Result of the hypothetical-index probe. Survives re-detection, so a "
             "recommendation the planner rejects stays demoted.",
    )
    planner_note = fields.Text()

    target_kind = fields.Selection(
        [
            ("table", "Table"),
            ("model", "Model"),
            ("field", "Field"),
            ("index", "Index"),
            ("cron", "Scheduled action"),
            ("bundle", "Asset bundle"),
            ("config", "Setting"),
            ("other", "Other"),
        ],
        default="other",
    )
    target_ref = fields.Char("Target", index=True)
    metric = fields.Char("Measured value")
    threshold = fields.Char("Threshold")
    recommendation = fields.Text()

    fix_kind = fields.Selection(
        [
            ("none", "None"),
            ("create_index", "Create index"),
            ("analyze", "Run ANALYZE"),
            ("vacuum", "Run VACUUM"),
            ("drop_index", "Drop index"),
            ("config", "Config change"),
        ],
        default="none",
    )
    fix_sql = fields.Text("Fix statement")
    fixable = fields.Boolean(compute="_compute_fixable", store=True)

    status = fields.Selection(
        [
            ("open", "Open"),
            ("acknowledged", "Acknowledged"),
            ("snoozed", "Snoozed"),
            ("resolved", "Resolved"),
            ("ignored", "Ignored"),
        ],
        default="open",
        required=True,
        tracking=True,
    )
    snooze_until = fields.Date()

    first_seen = fields.Datetime(readonly=True)
    last_seen = fields.Datetime(readonly=True)
    occurrence_count = fields.Integer(default=1, readonly=True)
    fix_applied_on = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("detector_target_uniq", "unique(detector_code, target_ref)",
         "A finding already exists for this detector and target."),
    ]

    @api.depends("severity")
    def _compute_severity_order(self):
        order = {"critical": 0, "warning": 1, "info": 2}
        for f in self:
            f.severity_order = order.get(f.severity, 9)

    @api.depends("fix_kind", "fix_sql")
    def _compute_fixable(self):
        for f in self:
            f.fixable = (
                f.fix_kind in ("create_index", "analyze", "vacuum")
                and bool(f.fix_sql)
                and is_safe_maintenance_sql(f.fix_sql)
            )

    # ------------------------------------------------------------------
    # Registration from a run
    # ------------------------------------------------------------------
    @api.model
    def _ingest_finding(self, run, detector, row):
        code = detector["code"]
        ref = row.get("target_ref") or ""
        now = fields.Datetime.now()
        default_name = "%s — %s" % (detector["title"], ref) if ref else detector["title"]
        vals = {
            "run_id": run.id,
            "detector_code": code,
            "category": detector["group"],
            "severity": row.get("severity") or detector.get("severity", "warning"),
            "name": row.get("name") or default_name,
            "target_kind": row.get("target_kind", "other"),
            "target_ref": ref,
            "metric": row.get("metric"),
            "threshold": row.get("threshold") or detector.get("threshold"),
            "recommendation": row.get("recommendation"),
            "fix_kind": row.get("fix_kind", "none"),
            "fix_sql": row.get("fix_sql"),
            "evidence": row.get("evidence"),
            "depth": detector.get("depth", "quick"),
            "last_seen": now,
        }
        existing = self.search([("detector_code", "=", code), ("target_ref", "=", ref)], limit=1)
        # A planner verdict is a property of the recommendation, not of this run:
        # re-detecting the same thing must not undo it.
        if existing and existing.planner_verdict == "unused":
            vals["severity"] = "info"
            vals["evidence"] = "\n\n".join(
                filter(None, [vals.get("evidence"), existing.planner_note]))
        if existing:
            reopened = existing.status == "resolved"
            if reopened:
                vals["status"] = "open"
            if existing.status == "snoozed" and existing.snooze_until and existing.snooze_until <= fields.Date.today():
                vals["status"] = "open"
            existing.write(vals)
            existing.occurrence_count += 1
            if reopened:
                existing.message_post(body=_("Detected again after being resolved."))
            return existing
        vals.update(first_seen=now, occurrence_count=1, status="open")
        return self.create(vals)

    # ------------------------------------------------------------------
    # Status actions
    # ------------------------------------------------------------------
    def action_acknowledge(self):
        self.write({"status": "acknowledged"})

    def action_ignore(self):
        self.write({"status": "ignored"})

    def action_reopen(self):
        self.write({"status": "open"})

    def action_snooze_30d(self):
        self.write({
            "status": "snoozed",
            "snooze_until": fields.Date.add(fields.Date.today(), days=30),
        })

    # ------------------------------------------------------------------
    # Safe fix
    # ------------------------------------------------------------------
    def action_apply_fix(self):
        self.ensure_one()
        if not self.env.user._is_system():
            raise AccessError(_("Only a Settings administrator can apply fixes."))
        if not self.fixable:
            raise UserError(_("This finding has no safe automatic fix."))
        sql = (self.fix_sql or "").strip().rstrip(";").strip()
        if not is_safe_maintenance_sql(sql):
            raise UserError(_("The fix statement is not on the allow-list and was not run."))

        dbname = self.env.cr.dbname
        cnx = odoo.sql_db.db_connect(dbname)
        with cnx.cursor() as maint_cr:
            # CREATE INDEX CONCURRENTLY and VACUUM cannot run inside a transaction.
            maint_cr._cnx.rollback()
            maint_cr._cnx.autocommit = True
            try:
                maint_cr.execute(sql)
            except Exception as exc:
                _logger.exception("perf.audit fix failed for finding %s", self.id)
                self.message_post(body=_("Fix failed: %s") % exc)
                raise UserError(_("The fix could not be applied:\n\n%s") % exc)
            finally:
                # The pool hands this connection to somebody else next; it must
                # not still be in autocommit when that happens.
                maint_cr._cnx.autocommit = False

        self.write({"fix_applied_on": fields.Datetime.now(), "status": "resolved"})
        self.message_post(
            body=Markup("<p>%s</p><pre>%s</pre>") % (_("Applied safe fix:"), sql)
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("Fix applied. Re-run the audit to confirm."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
