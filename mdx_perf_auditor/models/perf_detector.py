"""The detector catalog as real records.

Enabling and disabling checks used to mean editing a comma-separated
``ir.config_parameter`` by hand. Every detector the code declares is mirrored
into a row here on upgrade, so an administrator can see what the module actually
checks, read what each one costs, and switch one off without touching SQL.
"""

import logging
import operator as _op

from odoo import _, api, fields, models

_SEARCH_OPS = {">": _op.gt, ">=": _op.ge, "<": _op.lt, "<=": _op.le,
               "=": _op.eq, "!=": _op.ne}

_logger = logging.getLogger(__name__)

# Thresholds each detector honours, shown on its form so the tuning knobs are
# discoverable instead of buried in the source.
DETECTOR_THRESHOLDS = {
    "A1": ["a1_min_table_bytes", "a1_audit_col_bytes", "a1_big_table_bytes"],
    "A2": ["a2_min_rows"],
    "A3": ["a3_min_dead", "a3_ratio"],
    "A4": ["a4_min_rows", "a4_days"],
    "A5": ["a5_min_bytes"],
    "A6": ["a6_min_rows", "a6_min_seq", "a6_mult"],
    "A7": ["a7_bytes"],
    "A8": ["a8_min_reads", "a8_ratio"],
    "A9": ["a9_min_rows"],
    "B6": ["b6_depth"],
    "B7": ["b7_min_rows"],
    "C1": ["c1_seconds"],
    "C3": ["c3_repeat"],
    "C6": ["c6_ms"],
    "D2": ["d2_min_samples"],
    "D5": ["d5_queue"],
    "D6": ["d6_backlog"],
    "E1": ["e1_mb"],
    "E2": ["e2_modules"],
    "G1": ["g_min_ms", "g_top_n", "g1_min_rows"],
    "G2": ["g2_factor", "g2_min_rows"],
    "G4": ["g4_min_loops"],
    "H1": ["h_min_hours", "h1_mb_per_day"],
    "H2": ["h2_factor"],
    "J1": ["j_min_bytes", "j_max_bytes", "j_top_n", "j1_pct"],
    "J2": ["j2_density", "j2_min_bytes"],
}


class PerfDetector(models.Model):
    _name = "perf.detector"
    _description = "Performance Detector"
    _order = "code"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
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
    )
    depth = fields.Selection(
        [("quick", "Quick scan"), ("deep", "Deep analysis")], default="quick", required=True)
    default_severity = fields.Selection(
        [("critical", "Critical"), ("warning", "Warning"), ("info", "Advisory")])
    method = fields.Char(readonly=True)
    active = fields.Boolean(default=True, help="Uncheck to skip this detector on every run.")
    threshold_keys = fields.Char(
        readonly=True,
        help="ir.config_parameter keys, prefixed mdx_perf_auditor.threshold., that tune this check.")
    open_finding_count = fields.Integer(
        compute="_compute_open_finding_count", search="_search_open_finding_count")

    _code_uniq = models.Constraint("unique(code)", "Detector codes are unique.")

    def _compute_open_finding_count(self):
        Finding = self.env["perf.audit.finding"]
        counts = {}
        if self.ids:
            counts = dict(Finding._read_group(
                [("detector_code", "in", self.mapped("code")),
                 ("status", "in", ("open", "acknowledged"))],
                groupby=["detector_code"],
                aggregates=["__count"],
            ))
        for rec in self:
            rec.open_finding_count = counts.get(rec.code, 0)

    def _search_open_finding_count(self, operator, value):
        """Live count off another model, so it is searched rather than stored —
        a stored copy would go stale the moment a finding is acknowledged."""
        compare = _SEARCH_OPS.get(operator)
        if not compare:
            return []
        counts = dict(self.env["perf.audit.finding"]._read_group(
            [("status", "in", ("open", "acknowledged"))],
            groupby=["detector_code"], aggregates=["__count"]))
        codes = [
            rec.code for rec in self.with_context(active_test=False).search([])
            if compare(counts.get(rec.code, 0), value)
        ]
        return [("code", "in", codes)]

    def action_view_findings(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Findings — %s") % self.code,
            "res_model": "perf.audit.finding",
            "view_mode": "list,form",
            "domain": [("detector_code", "=", self.code)],
        }

    # ------------------------------------------------------------------
    def _register_hook(self):
        """Keep the catalog in step with the code on every registry load, so an
        upgrade that adds or removes a detector needs no migration script and the
        list is never empty when somebody opens it."""
        super()._register_hook()
        try:
            self._sync_if_needed()
        except Exception:
            _logger.exception("mdx_perf_auditor: detector catalog sync skipped")

    @api.model
    def _sync_if_needed(self):
        """Cheap guard so an upgrade that adds or drops a detector heals itself
        without a migration script."""
        declared = self.env["perf.audit.run"].browse()._get_detectors()
        if self.with_context(active_test=False).search_count([]) != len(declared):
            self._sync()

    @api.model
    def _sync(self):
        """Mirror the code-declared catalog into records. Idempotent; keeps the
        administrator's active flag."""
        declared = self.env["perf.audit.run"].browse()._get_detectors()
        existing = {d.code: d for d in self.with_context(active_test=False).search([])}
        for det in declared:
            vals = {
                "code": det["code"],
                "name": det.get("title") or det["code"],
                "category": det["group"],
                "depth": det.get("depth", "quick"),
                "default_severity": det.get("severity"),
                "method": det.get("method"),
                "threshold_keys": ", ".join(DETECTOR_THRESHOLDS.get(det["code"], [])) or False,
            }
            rec = existing.get(det["code"])
            if rec:
                rec.write(vals)  # never touches `active`
            else:
                self.create(vals)
        stale = set(existing) - {d["code"] for d in declared}
        if stale:
            self.with_context(active_test=False).search(
                [("code", "in", list(stale))]).unlink()
        _logger.info("mdx_perf_auditor: detector catalog synced (%s entries)", len(declared))


def post_init_sync(env):
    env["perf.detector"]._sync()
