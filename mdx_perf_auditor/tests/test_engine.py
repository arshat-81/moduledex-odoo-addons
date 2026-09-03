"""Engine: the run lifecycle, finding ingest, scoring and the fix allow-list."""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEngine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Run = cls.env["perf.audit.run"]
        cls.Finding = cls.env["perf.audit.finding"]
        cls.det = {"code": "T1", "group": "db", "title": "Test detector",
                   "severity": "warning", "depth": "quick"}

    def _row(self, ref="tbl.col", **kw):
        row = {"target_ref": ref, "metric": "m", "threshold": "t",
               "recommendation": "do the thing", "fix_kind": "none"}
        row.update(kw)
        return row

    def test_run_completes_and_scores(self):
        run = self.Run.action_run_now()
        rec = self.Run.browse(run["res_id"])
        self.assertEqual(rec.state, "done")
        self.assertGreater(rec.detectors_run, 0)
        self.assertTrue(0 <= rec.health_score <= 100)
        self.assertEqual(rec.depth, "quick")

    def test_deep_runs_more_detectors_than_quick(self):
        quick = self.Run.browse(self.Run.action_run_now("manual", "quick")["res_id"])
        deep = self.Run.browse(self.Run.action_run_now("manual", "deep")["res_id"])
        self.assertGreater(deep.detectors_run, quick.detectors_run)
        self.assertEqual(deep.depth, "deep")

    def test_ingest_is_idempotent_and_counts_occurrences(self):
        run = self.Run.create({"name": "t", "trigger": "manual"})
        first = self.Finding._ingest_finding(run, self.det, self._row())
        again = self.Finding._ingest_finding(run, self.det, self._row())
        self.assertEqual(first, again, "same detector+target must reuse the finding")
        self.assertEqual(again.occurrence_count, 2)

    def test_ignored_status_survives_redetection(self):
        run = self.Run.create({"name": "t", "trigger": "manual"})
        f = self.Finding._ingest_finding(run, self.det, self._row())
        f.action_ignore()
        self.Finding._ingest_finding(run, self.det, self._row())
        self.assertEqual(f.status, "ignored", "re-detecting must not un-ignore")

    def test_scores_and_counts_agree(self):
        rec = self.Run.browse(self.Run.action_run_now()["res_id"])
        openf = self.Finding.search_count([("status", "in", ("open", "acknowledged"))])
        self.assertEqual(
            rec.critical_count + rec.warning_count + rec.info_count, openf,
            "the header counts must be the same snapshot the score came from")

    def test_fix_allow_list_rejects_anything_else(self):
        run = self.Run.create({"name": "t", "trigger": "manual"})
        for i, bad in enumerate((
            "DROP TABLE res_users",
            "DELETE FROM res_users",
            "UPDATE res_users SET login='x'",
            "CREATE INDEX ix ON t (c)",          # not CONCURRENTLY -> takes a lock
            "ANALYZE; DROP TABLE x",             # statement stacking
            'ANALYZE "t"; DROP TABLE x',         # stacking after a valid verb
            'ANALYZE "t" -- drop it later',      # trailing comment
            'ANALYZE "t"/* comment */',
            "  vacuum full res_users",           # VACUUM FULL locks the table
            'ANALYZE res_users',                 # unquoted identifier
        )):
            f = self.Finding._ingest_finding(
                run, dict(self.det, code="TX%d" % i),
                self._row(ref="bad%d" % i, fix_kind="create_index", fix_sql=bad))
            self.assertFalse(f.fixable, "must not offer to run: %s" % bad)

    def test_fix_allow_list_accepts_the_three_safe_forms(self):
        run = self.Run.create({"name": "t", "trigger": "manual"})
        for i, good in enumerate((
            'ANALYZE "res_users"',
            'VACUUM (ANALYZE) "res_users"',
            'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_t" ON "res_users" ("id")',
        )):
            f = self.Finding._ingest_finding(
                run, dict(self.det, code="TS%d" % i),
                self._row(ref="ok%d" % i, fix_kind="analyze", fix_sql=good))
            self.assertTrue(f.fixable, "should be applicable: %s" % good)

    def test_threshold_falls_back_on_junk(self):
        run = self.Run.create({"name": "t", "trigger": "manual"})
        self.env["ir.config_parameter"].sudo().set_param(
            "mdx_perf_auditor.threshold.unit_probe", "not-a-number")
        self.assertEqual(run._threshold("unit_probe", 42), 42)
        self.env["ir.config_parameter"].sudo().set_param(
            "mdx_perf_auditor.threshold.unit_probe", "7")
        self.assertEqual(run._threshold("unit_probe", 42), 7)

    def test_disabled_detector_is_skipped(self):
        det = self.env["perf.detector"].search([("code", "=", "A1")], limit=1)
        if not det:
            self.skipTest("catalog not populated")
        det.active = False
        run = self.Run.browse(self.Run.action_run_now()["res_id"])
        self.assertNotIn("A1", run._disabled_detectors() and [] or [])
        self.assertIn("A1", run._disabled_detectors())

    def test_dashboard_payload_shape(self):
        self.Run.action_run_now()
        d = self.Run.get_dashboard_data()
        for key in ("has_run", "run", "trend", "categories", "findings",
                    "capture", "biggest_tables", "severity_labels"):
            self.assertIn(key, d)
        for key in ("health_score", "scores", "counts", "open_total", "depth"):
            self.assertIn(key, d["run"])
