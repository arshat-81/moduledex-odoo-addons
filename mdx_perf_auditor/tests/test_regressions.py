"""One test per bug that actually shipped. Each name says what broke."""

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRegressions(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Run = cls.env["perf.audit.run"]
        cls.Finding = cls.env["perf.audit.finding"]
        cls.Snap = cls.env["perf.db.snapshot"]

    # -- bug: _register collided with the ORM's reserved class flag ----------
    def test_ingest_entry_point_is_callable(self):
        self.assertTrue(callable(getattr(self.Finding, "_ingest_finding", None)))
        self.assertNotIsInstance(
            getattr(type(self.Finding), "_register", None), type(self._ingest_probe),
            "do not name a method _register — the ORM owns that attribute")

    def _ingest_probe(self):
        pass

    # -- bug: _order can be a @property (account patches res.partner) --------
    def test_order_read_as_property_does_not_crash(self):
        run = self.Run.create({"name": "t", "trigger": "manual"})

        class Fake:
            _order = property(lambda self: "should not be reachable")
            _log_access = property(lambda self: False)

        self.assertEqual(run._model_attr("res.users", Fake, "_order", "id"),
                         self.env["res.users"]._order)
        self.assertIsInstance(run._model_attr("res.users", Fake, "_log_access", True), bool)
        # and the detectors that read it survive a full pass
        for method in ("_detect_b1_computed_order", "_detect_a2_unindexed_order",
                       "_detect_b7_log_access"):
            getattr(run, method)()

    # -- bug: planner verdict was wiped by the next ingest -------------------
    def test_planner_verdict_survives_redetection(self):
        run = self.Run.create({"name": "t", "trigger": "manual"})
        det = {"code": "TP", "group": "db", "title": "probe",
               "severity": "critical", "depth": "quick"}
        row = {"target_ref": "tp.probe", "metric": "m", "fix_kind": "create_index",
               "fix_sql": 'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_tp" ON "res_users" ("id")'}
        f = self.Finding._ingest_finding(run, det, row)
        f.write({"planner_verdict": "unused", "planner_note": "would not be used",
                 "severity": "info"})
        self.Finding._ingest_finding(run, det, row)
        f.invalidate_recordset()
        self.assertEqual(f.severity, "info", "a rejected index must stay demoted")
        self.assertIn("would not be used", f.evidence or "")

    # -- bug: growth was extrapolated from minutes of history ----------------
    def test_growth_refuses_a_span_that_is_too_short(self):
        run = self.Run.create({"name": "t", "trigger": "manual", "depth": "deep"})
        self.Snap.search([]).unlink()
        now = fields.Datetime.now()
        for minutes, mb in ((4, 100), (0, 400)):
            self.Snap.create({
                "date": fields.Datetime.subtract(now, minutes=minutes),
                "table_json": [{"name": "t_probe", "bytes": mb * 1024 * 1024,
                                "human": "%d MB" % mb, "rows": 1}]})
        self.assertEqual(run._snapshot_pair(), (None, None))
        self.assertFalse(run._detect_h1_growth())
        self.assertTrue(run._detect_h0_no_history(), "H0 should explain the gap")

    def test_growth_rate_is_arithmetically_right(self):
        run = self.Run.create({"name": "t", "trigger": "manual", "depth": "deep"})
        self.Snap.search([]).unlink()
        now = fields.Datetime.now()
        for hours, mb in ((12, 100), (0, 110)):   # 10 MB in 12 h == 20 MB/day
            self.Snap.create({
                "date": fields.Datetime.subtract(now, hours=hours),
                "table_json": [{"name": "t_probe", "bytes": mb * 1024 * 1024,
                                "human": "%d MB" % mb, "rows": 1}]})
        rows = run._detect_h1_growth()
        self.assertTrue(rows, "10 MB in 12 h should clear the 20 MB/day floor")
        self.assertIn("20 MB/day", rows[0]["metric"])

    # -- bug: with no capture, everything in ir.profile was treated as ours --
    def test_capture_scoped_to_its_own_window(self):
        param = self.env["ir.config_parameter"].sudo()
        param.set_param("mdx_perf_auditor.capture_started", "")
        self.assertEqual(self.env["perf.capture"].status()["profiles"], 0)
        self.assertFalse(self.env["perf.capture"].captured_profiles())

    # -- bug: run header counts drifted away from the score ------------------
    def test_counts_are_snapshotted_not_live(self):
        first = self.Run.browse(self.Run.action_run_now()["res_id"])
        snapshot = (first.critical_count, first.warning_count, first.info_count)
        self.Run.action_run_now()          # a later run steals the run_id links
        first.invalidate_recordset()
        self.assertEqual(
            (first.critical_count, first.warning_count, first.info_count), snapshot,
            "an old run must keep the counts it was scored on")

    # -- bug: mass auto-resolve posted one chatter message per finding -------
    def test_bulk_resolve_does_not_flood_the_chatter(self):
        run = self.Run.create({"name": "t", "trigger": "manual"})
        det = {"code": "TB", "group": "db", "title": "bulk", "severity": "info"}
        for i in range(40):
            self.Finding._ingest_finding(run, det, {"target_ref": "bulk_%d" % i})
        made = self.Finding.search([("detector_code", "=", "TB")])
        self.assertEqual(len(made), 40)
        before = self.env["mail.message"].search_count(
            [("model", "=", "perf.audit.finding"), ("res_id", "in", made.ids)])
        run2 = self.Run.create({"name": "t2", "trigger": "manual"})
        run2._run()                          # TB no longer reported -> all resolve
        after = self.env["mail.message"].search_count(
            [("model", "=", "perf.audit.finding"), ("res_id", "in", made.ids)])
        self.assertEqual(after, before, "40 resolutions must not write 40 messages")

    # -- bug: capture hook rode on a core private method with no guard -------
    def test_capture_reports_whether_the_seam_exists(self):
        self.assertIn("supported", self.env["perf.capture"].status())

    # -- bug: SET LOCAL statement_timeout outlived the EXPLAIN savepoint -----
    def test_explain_does_not_leak_the_statement_timeout(self):
        """SET LOCAL is scoped to the transaction, not the savepoint.

        Releasing the savepoint after a successful EXPLAIN used to leave the 5 s
        cap in force for every later detector, which aborts the long pgstattuple
        scans in J1/J2 on exactly the databases that need them."""
        run = self.Run.create({"name": "t", "trigger": "manual", "depth": "deep"})
        self.env.cr.execute("SHOW statement_timeout")
        before = self.env.cr.fetchone()[0]
        plan = run._explain('SELECT id FROM res_users ORDER BY id LIMIT 1')
        self.assertTrue(plan, "a trivial read should be explainable")
        self.env.cr.execute("SHOW statement_timeout")
        self.assertEqual(
            self.env.cr.fetchone()[0], before,
            "EXPLAIN must restore statement_timeout before the next detector runs")

    # -- bug: G1 recommended an index that already existed (even the PK) ----
    def test_g1_never_recommends_an_existing_index(self):
        run = self.Run.create({"name": "t", "trigger": "manual", "depth": "deep"})
        leading = run._leading_index_cols("res_users")
        self.assertIn("id", leading, "the primary key is a leading index column")
        self.assertIn("login", run._real_columns("res_users"))
        self.assertFalse(
            {"no_such_column_xyz"} & run._real_columns("res_users"),
            "_real_columns must not invent columns")

    # -- bug: H1 capped at 10 rows *after* sorting by name, not growth ------
    def test_h1_keeps_the_fastest_growing_tables(self):
        now = fields.Datetime.now()
        old, new = [], []
        # 12 tables; the fastest grower is deliberately last alphabetically.
        for i in range(12):
            name = "t_%02d" % i
            old.append({"name": name, "bytes": 0})
            new.append({"name": name, "bytes": (i + 1) * 400 * 1024 * 1024})
        self.Snap.create({
            "date": now - relativedelta(days=1),
            "table_json": old, "db_size_bytes": 0})
        self.Snap.create({"date": now, "table_json": new, "db_size_bytes": 1})
        run = self.Run.create({"name": "t", "trigger": "manual", "depth": "deep"})
        rows = run._detect_h1_growth()
        self.assertEqual(len(rows), 10, "H1 caps at ten rows")
        self.assertEqual(
            rows[0]["target_ref"], "t_11",
            "the fastest-growing table must survive the cap, not the first by name")
        self.assertNotIn(
            "t_00", [r["target_ref"] for r in rows],
            "the slowest grower must be the one dropped")
        self.assertNotIn("_per_day", rows[0], "sort key must not leak into the finding")

    # -- bug: A2 asked the field, not PostgreSQL, whether a sort was indexed --
    def test_a2_respects_composite_indexes(self):
        """ir_model_data orders by (module, name) and carries a unique index on
        exactly that pair. Asking each column whether it is individually indexed
        reported three problems that do not exist, on every Odoo database."""
        run = self.Run.create({"name": "t", "trigger": "manual"})
        by_table = run._index_columns_by_table()
        self.assertIn(["module", "name"], by_table.get("ir_model_data", []),
                      "the composite unique index must be visible to the detector")
        self.assertEqual(
            run._order_columns_served("ir_model_data", ["module", "name"]), 2,
            "an index on (module, name) fully serves ORDER BY module, name")
        self.assertEqual(
            run._order_columns_served("ir_model_data", ["module"]), 1,
            "it serves the leading column on its own too")
        self.assertEqual(
            run._order_columns_served("ir_model_data", ["name"]), 0,
            "but it cannot serve a sort that starts with the second key")
        # And it reports the sort once, with an index that would actually work:
        # three single-column indexes cannot serve ORDER BY module, model, name.
        hits = [f for f in run._detect_a2_unindexed_order()
                if f["target_ref"].startswith("ir_model_data")]
        self.assertLessEqual(len(hits), 1, "one sort is one finding, not one per column")
        for f in hits:
            self.assertIn(",", f["fix_sql"], "the fix must be a composite index")
            for col in ("module", "model", "name"):
                self.assertIn('"%s"' % col, f["fix_sql"],
                              "the index has to cover the whole ORDER BY")
