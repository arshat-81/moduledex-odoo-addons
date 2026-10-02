"""Every detector must run against a live database and hand back well-formed
rows. Most find nothing on a healthy test database — that is the point; what is
being asserted is that none of them crashes or returns junk."""

from odoo.tests import TransactionCase, tagged

REQUIRED_KEYS = {"target_ref"}
ALLOWED_KEYS = {
    "target_kind", "target_ref", "severity", "name", "metric", "threshold",
    "recommendation", "fix_kind", "fix_sql", "evidence",
}
VALID_SEVERITY = {"critical", "warning", "info"}
VALID_FIX = {"none", "create_index", "analyze", "vacuum", "drop_index", "config"}


@tagged("post_install", "-at_install")
class TestDetectors(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.audit = cls.env["perf.audit.run"].create(
            {"name": "unit", "trigger": "manual", "depth": "deep"})

    def test_every_detector_runs_and_returns_valid_rows(self):
        failures, checked = [], 0
        for det in self.audit._get_detectors():
            checked += 1
            try:
                rows = getattr(self.audit, det["method"])() or []
            except Exception as exc:
                failures.append("%s raised %r" % (det["code"], exc))
                continue
            self.assertIsInstance(rows, list, "%s must return a list" % det["code"])
            for row in rows:
                self.assertIsInstance(row, dict, "%s row must be a dict" % det["code"])
                extra = set(row) - ALLOWED_KEYS
                self.assertFalse(extra, "%s emitted unknown keys %s" % (det["code"], extra))
                self.assertTrue(REQUIRED_KEYS <= set(row),
                                "%s row is missing target_ref" % det["code"])
                if row.get("severity"):
                    self.assertIn(row["severity"], VALID_SEVERITY, det["code"])
                if row.get("fix_kind"):
                    self.assertIn(row["fix_kind"], VALID_FIX, det["code"])
        self.assertFalse(failures, "\n".join(failures))
        self.assertGreaterEqual(checked, 40, "the catalog shrank unexpectedly")

    def test_detector_codes_are_unique(self):
        codes = [d["code"] for d in self.audit._get_detectors()]
        self.assertEqual(len(codes), len(set(codes)), "duplicate detector code")

    def test_every_detector_declares_a_known_group(self):
        valid = dict(self.env["perf.audit.finding"]._fields["category"].selection)
        for det in self.audit._get_detectors():
            self.assertIn(det["group"], valid, det["code"])

    def test_catalog_records_match_the_code(self):
        self.env["perf.detector"]._sync()
        declared = {d["code"] for d in self.audit._get_detectors()}
        stored = set(self.env["perf.detector"].with_context(active_test=False)
                     .search([]).mapped("code"))
        self.assertEqual(declared, stored)

    def test_generated_index_names_fit_postgres(self):
        """Index identifiers over 63 bytes are silently truncated by PostgreSQL,
        which would make the fix collide with another index."""
        from ..models.detectors_database import _ix_name
        long_name = _ix_name("a_very_long_table_name_that_keeps_going_and_going", "some_column_id")
        self.assertLessEqual(len(long_name), 63)
        self.assertEqual(_ix_name("sale_order", "partner_id"), "ix_sale_order_partner_id")

    def test_model_deep_dive_renders(self):
        model = self.env["ir.model"].search([("model", "=", "res.users")], limit=1)
        wizard = self.env["perf.model.analysis"].create({"model_id": model.id})
        html = wizard.report_html or ""
        self.assertIn("Storage", html)
        self.assertIn("Fields", html)
        self.assertNotIn("property object", html)
