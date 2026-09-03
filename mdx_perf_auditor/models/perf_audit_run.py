import logging
import time
from collections import defaultdict

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.tools import SQL

_logger = logging.getLogger(__name__)

# Health-score penalty per open finding, by severity.
SEVERITY_WEIGHT = {"critical": 9.0, "warning": 3.0, "info": 0.6}

# Typographic characters the PDF pipeline cannot be trusted to carry.
PDF_TRANSLITERATE = {
    "\u2014": " - ", "\u2013": "-", "\u2012": "-", "\u2212": "-",
    "\u00b7": "-", "\u2022": "*", "\u2026": "...",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u00a0": " ", "\u202f": " ", "\u2192": "->", "\u2190": "<-",
    "\u2265": ">=", "\u2264": "<=", "\u00d7": "x", "\u00b0": " deg",
    "\u2713": "ok", "\u25b2": "^", "\u25bc": "v",
}

# A broken detector can fail once per finding; keep the report readable.
_MAX_ERRORS = 25
# Above this many auto-resolved findings, log a summary instead of N chatter posts.
_CHATTER_LIMIT = 25

CATEGORIES = [
    ("db", "Database"),
    ("orm", "ORM & model design"),
    ("runtime", "Runtime"),
    ("jobs", "Background jobs"),
    ("frontend", "Assets & frontend"),
    ("config", "Configuration"),
]
CATEGORY_CODES = [c[0] for c in CATEGORIES]


class PerfAuditRun(models.Model):
    _name = "perf.audit.run"
    _description = "Performance Audit Run"
    _order = "date desc, id desc"

    name = fields.Char(readonly=True)
    date = fields.Datetime(default=fields.Datetime.now, readonly=True, index=True)
    trigger = fields.Selection(
        [("manual", "Manual"), ("cron", "Scheduled")],
        default="manual",
        readonly=True,
    )
    state = fields.Selection(
        [("running", "Running"), ("done", "Done"), ("failed", "Failed")],
        default="running",
        readonly=True,
    )
    depth = fields.Selection(
        [("quick", "Quick scan"), ("deep", "Deep analysis")],
        default="quick",
        required=True,
        readonly=True,
        index=True,
    )
    duration = fields.Float("Duration (s)", digits=(9, 2), readonly=True)
    health_score = fields.Integer(readonly=True, aggregator="avg")

    score_db = fields.Integer("Database", readonly=True)
    score_orm = fields.Integer("ORM", readonly=True)
    score_runtime = fields.Integer("Runtime", readonly=True)
    score_jobs = fields.Integer("Jobs", readonly=True)
    score_frontend = fields.Integer("Frontend", readonly=True)
    score_config = fields.Integer("Configuration", readonly=True)

    detectors_run = fields.Integer("Detectors run", readonly=True)
    detector_errors = fields.Text("Detector errors", readonly=True)

    finding_ids = fields.One2many(
        "perf.audit.finding", "run_id", string="Findings last seen in this run",
        help="A later run re-attaches a finding to itself, so this shrinks over time. "
             "The counts below are the snapshot taken when this run finished.")
    # Snapshotted at the end of the run, from the same set the score is derived
    # from — otherwise an old run shows a score with no findings behind it.
    critical_count = fields.Integer(readonly=True)
    warning_count = fields.Integer(readonly=True)
    info_count = fields.Integer(readonly=True)

    # ------------------------------------------------------------------
    # Detector catalog. Each detector file extends this via super().
    # A detector entry:
    #   code:      stable id, e.g. "A1"
    #   group:     one of CATEGORY_CODES
    #   title:     default finding title
    #   severity:  default severity when the detector does not set one
    #   method:    name of the method returning a list of finding dicts
    # ------------------------------------------------------------------
    def _get_detectors(self):
        return []

    def _post_detect(self, ran_codes):
        """Deep-only refinement pass, run after all detectors. Overridden by
        modules that can validate or enrich what the detectors produced."""
        return

    def _disabled_detectors(self):
        """Codes switched off, from the detector catalog plus the legacy
        config-parameter escape hatch."""
        disabled = set(
            self.env["perf.detector"].with_context(active_test=False)
            .search([("active", "=", False)]).mapped("code"))
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "mdx_perf_auditor.disabled_detectors", "") or ""
        return disabled | {c.strip().upper() for c in raw.split(",") if c.strip()}

    def _threshold(self, key, default):
        val = self.env["ir.config_parameter"].sudo().get_param(
            "mdx_perf_auditor.threshold.%s" % key
        )
        if val in (None, False, ""):
            return default
        try:
            return type(default)(val)
        except (TypeError, ValueError):
            return default

    def _model_attr(self, model_name, model_cls, attr, default):
        """Read a model attribute safely off a registry class.

        Some modules declare these as properties — ``account`` makes
        ``res.partner._order`` one, so it can prepend ``customer_rank DESC`` — and
        reading a property off the class hands back the descriptor rather than the
        value. Fall back to resolving it through a recordset in that case.
        """
        value = getattr(model_cls, attr, None)
        if isinstance(value, type(default)):
            return value
        try:
            value = getattr(self.env[model_name], attr, default)
        except Exception:
            return default
        return value if isinstance(value, type(default)) else default

    # ------------------------------------------------------------------
    # Read-only catalog helper
    # ------------------------------------------------------------------
    def _pg(self, query):
        """Run a read-only query and return a list of dicts. ``query`` is an
        :class:`odoo.tools.SQL` object built by the caller."""
        self.env.cr.execute(query)
        return self.env.cr.dictfetchall()

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    @api.model
    def action_run_now(self, trigger="manual", depth="quick"):
        label = _("Deep analysis") if depth == "deep" else _("Health audit")
        run = self.create({
            "name": "%s — %s" % (label, fields.Datetime.to_string(fields.Datetime.now())),
            "trigger": trigger,
            "depth": depth if depth in ("quick", "deep") else "quick",
        })
        run._run()
        return {
            "type": "ir.actions.act_window",
            "res_model": "perf.audit.run",
            "res_id": run.id,
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "current",
        }

    @api.model
    def action_run_deep(self):
        return self.action_run_now(trigger="manual", depth="deep")

    @api.model
    def cron_run_audit(self):
        before = self.env["perf.audit.finding"].sudo().search([
            ("status", "in", ("open", "acknowledged")),
        ]).ids
        self.action_run_now(trigger="cron")
        run = self.search([("trigger", "=", "cron")], limit=1)
        if run:
            run._notify_watchers(set(before))

    def _run(self):
        self.ensure_one()
        start = time.monotonic()
        Finding = self.env["perf.audit.finding"].sudo()
        self.env["perf.detector"]._sync_if_needed()
        disabled = self._disabled_detectors()
        errors = []
        seen_keys = set()
        ok_detector_codes = set()
        deep = self.depth == "deep"

        for det in self._get_detectors():
            code = det["code"]
            if code in disabled:
                continue
            if det.get("depth") == "deep" and not deep:
                continue
            try:
                with self.env.cr.savepoint():
                    rows = getattr(self, det["method"])() or []
            except Exception as exc:  # one broken detector must not abort the run
                _logger.exception("perf.audit detector %s failed", code)
                errors.append("%s: %s" % (code, exc))
                continue
            ok_detector_codes.add(code)
            for row in rows:
                key = (code, row.get("target_ref") or "")
                seen_keys.add(key)
                try:
                    Finding._ingest_finding(self, det, row)
                except Exception as exc:
                    _logger.exception("perf.audit could not record finding for %s", code)
                    if len(errors) < _MAX_ERRORS:
                        errors.append("%s (record): %s" % (code, exc))

        # Deep passes may refine what the detectors produced (e.g. asking the
        # planner whether a recommended index would actually be used).
        if deep:
            try:
                self._post_detect(ok_detector_codes)
            except Exception as exc:
                _logger.exception("perf.audit post-detection pass failed")
                errors.append("post: %s" % exc)

        # Anything a successfully-run detector no longer reports is resolved.
        stale = Finding.search([
            ("detector_code", "in", list(ok_detector_codes)),
            ("status", "in", ("open", "acknowledged")),
        ])
        gone = stale.filtered(
            lambda f: (f.detector_code, f.target_ref or "") not in seen_keys)
        if gone:
            # One write and one message for the whole batch: resolving a few
            # hundred findings must not turn into a few hundred chatter posts.
            gone.write({"status": "resolved", "last_seen": fields.Datetime.now()})
            if len(gone) <= _CHATTER_LIMIT:
                for finding in gone:
                    finding.message_post(body=_("No longer detected — marked resolved."))
            else:
                _logger.info(
                    "perf.audit resolved %s findings in run %s", len(gone), self.id)

        self._compute_scores()
        self.write({
            "state": "failed" if errors and not ok_detector_codes else "done",
            "duration": round(time.monotonic() - start, 2),
            "detectors_run": len(ok_detector_codes),
            "detector_errors": "\n".join(errors) or False,
        })

    def _notify_watchers(self, previously_open_ids):
        """Email the configured recipients when a scheduled run turns up
        something new that is worth interrupting somebody for."""
        self.ensure_one()
        param = self.env["ir.config_parameter"].sudo()
        raw = param.get_param("mdx_perf_auditor.alert_user_ids") or ""
        min_sev = param.get_param("mdx_perf_auditor.alert_severity") or "critical"
        user_ids = [int(i) for i in raw.split(",") if i.strip().isdigit()]
        if not user_ids:
            return
        wanted = ("critical",) if min_sev == "critical" else ("critical", "warning")
        fresh = self.env["perf.audit.finding"].sudo().search([
            ("status", "in", ("open", "acknowledged")),
            ("severity", "in", wanted),
            ("id", "not in", list(previously_open_ids)),
        ])
        if not fresh:
            return
        emails = [u.email for u in self.env["res.users"].sudo().browse(user_ids) if u.email]
        if not emails:
            _logger.warning("mdx_perf_auditor: alert recipients have no email address")
            return
        lines = "".join(
            "<li><b>[%s] %s</b><br/><span style='color:#666'>%s</span></li>" % (
                f.severity.upper(), escape(f.name or ""), escape(f.metric or ""))
            for f in fresh[:20])
        body = Markup(
            "<p>The scheduled audit found %s new finding(s). Health score is now "
            "<b>%s/100</b>.</p><ul>%s</ul>"
        ) % (len(fresh), self.health_score, Markup(lines))
        self.env["mail.mail"].sudo().create({
            "subject": _("Odoo health: %s new finding(s), score %s/100")
            % (len(fresh), self.health_score),
            "email_to": ",".join(emails),
            "body_html": body,
            "auto_delete": True,
        }).send()

    def _compute_scores(self):
        self.ensure_one()
        openf = self.env["perf.audit.finding"].sudo().search([
            ("status", "in", ("open", "acknowledged")),
        ])
        by_cat = defaultdict(float)
        by_sev = defaultdict(int)
        for f in openf:
            by_cat[f.category] += SEVERITY_WEIGHT.get(f.severity, 1.0)
            by_sev[f.severity] += 1
        vals = {
            "critical_count": by_sev["critical"],
            "warning_count": by_sev["warning"],
            "info_count": by_sev["info"],
        }
        for cat in CATEGORY_CODES:
            vals["score_%s" % cat] = max(0, round(100 - by_cat[cat]))
        vals["health_score"] = max(0, round(100 - sum(by_cat.values())))
        self.write(vals)

    # ------------------------------------------------------------------
    # Dashboard payload
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_data(self):
        history = self.search([("state", "=", "done")], limit=14)
        run, prev = history[:1], history[1:2]
        Finding = self.env["perf.audit.finding"].sudo()
        openf = Finding.search([("status", "in", ("open", "acknowledged"))])
        snap = self.env["perf.db.snapshot"].sudo().search([], limit=1)

        def as_dict(f):
            return {
                "id": f.id,
                "detector_code": f.detector_code,
                "name": f.name,
                "severity": f.severity,
                "category": f.category,
                "target_kind": f.target_kind,
                "target_ref": f.target_ref or "",
                "metric": f.metric or "",
                "threshold": f.threshold or "",
                "recommendation": f.recommendation or "",
                "fix_kind": f.fix_kind,
                "fix_sql": f.fix_sql or "",
                "evidence": f.evidence or "",
                "depth": f.depth or "quick",
                "fixable": f.fixable,
                "status": f.status,
                "occurrence_count": f.occurrence_count,
                "is_new": f.occurrence_count <= 1,
                "first_seen": fields.Datetime.to_string(f.first_seen) if f.first_seen else False,
            }

        counts = {
            cat: {
                sev: len(openf.filtered(lambda x: x.category == cat and x.severity == sev))
                for sev in ("critical", "warning", "info")
            }
            for cat in CATEGORY_CODES
        }
        resolved_recent = (
            Finding.search([("status", "=", "resolved"), ("run_id", "=", run.id)])
            if run else Finding.browse()
        )

        return {
            "has_run": bool(run),
            "capture": self.env["perf.capture"].status(),
            "run": {
                "id": run.id,
                "depth": run.depth if run else "quick",
                "date": fields.Datetime.to_string(run.date) if run else False,
                "health_score": run.health_score if run else 0,
                "prev_score": prev.health_score if prev else None,
                "delta": (run.health_score - prev.health_score) if (run and prev) else None,
                "duration": run.duration if run else 0,
                "detectors_run": run.detectors_run if run else 0,
                "detector_errors": bool(run.detector_errors) if run else False,
                "scores": {c: (run["score_%s" % c] if run else 0) for c in CATEGORY_CODES},
                "counts": counts,
                "open_total": len(openf),
            },
            "trend": [
                {"date": fields.Datetime.to_string(r.date), "score": r.health_score}
                for r in reversed(history)
            ],
            "categories": dict(CATEGORIES),
            "severity_labels": {"critical": _("Critical"), "warning": _("Warning"), "info": _("Advisory")},
            "findings": [as_dict(f) for f in openf.sorted(
                key=lambda f: (f.severity_order, f.category, f.detector_code))][:400],
            "resolved_recent": [
                {"id": f.id, "name": f.name, "category": f.category}
                for f in resolved_recent[:40]
            ],
            "biggest_tables": (snap.table_json or []) if snap else [],
            "db_size": snap.db_size_human if snap else False,
        }

    def pdf_text(self, value):
        """Return text that survives PDF generation on any server.

        wkhtmltopdf takes its input encoding from the process locale, and a
        server running with LANG=C or LANG=en_IN (no .UTF-8 suffix) decodes the
        document as latin-1 whatever <meta charset> says — an em dash comes out
        as three mojibake glyphs. We cannot fix a customer's locale from inside a
        module, so the report simply does not depend on one: typographic
        characters are transliterated to their ASCII equivalents, and anything
        else non-ASCII is dropped rather than rendered as garbage.
        """
        text = value if isinstance(value, str) else ("" if value is None else str(value))
        out = []
        for ch in text:
            if ord(ch) < 128:
                out.append(ch)
            else:
                out.append(PDF_TRANSLITERATE.get(ch, ""))
        return escape("".join(out))

    @api.model
    def dashboard_finding_action(self, finding_id, method):
        """Run a whitelisted status/fix action on a finding from the dashboard."""
        allowed = {"action_apply_fix", "action_acknowledge", "action_ignore",
                   "action_reopen", "action_snooze_30d"}
        if method not in allowed:
            return False
        finding = self.env["perf.audit.finding"].browse(finding_id)
        if not finding.exists():
            return False
        return getattr(finding, method)() or True
