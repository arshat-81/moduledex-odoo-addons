"""Group J — precise bloat measurement, plus the planner-validation pass.

pgstattuple gives a real bloat figure instead of the dead-tuple heuristic, at the
cost of a full table scan — so it is deep-only, size-capped and limited to the
biggest few tables.

hypopg lets us ask the planner "if this index existed, would you use it?" before
anybody builds it. Every index this module recommends gets that question put to
it, and recommendations the planner would ignore are demoted instead of shown as
work to do.
"""

import json
import logging
import re

from odoo import _, models
from odoo.tools import SQL

_logger = logging.getLogger(__name__)

_IX_TARGET = re.compile(r'ON\s+"([^"]+)"\s*\(([^)]+)\)', re.IGNORECASE)
_IX_COL = re.compile(r'"([^"]+)"')


class PerfAuditRunDeepDb(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "J1", "group": "db", "depth": "deep", "method": "_detect_j1_table_bloat",
             "title": _("Measured table bloat"), "severity": "warning"},
            {"code": "J2", "group": "db", "depth": "deep", "method": "_detect_j2_index_bloat",
             "title": _("Measured index bloat"), "severity": "info"},
        ]

    def _has_extension(self, name):
        return bool(self._pg(SQL("SELECT 1 FROM pg_extension WHERE extname = %s", name)))

    # ------------------------------------------------------------------
    # J — pgstattuple
    # ------------------------------------------------------------------
    def _bloat_candidates(self):
        floor = self._threshold("j_min_bytes", 8 * 1024 * 1024)
        ceiling = self._threshold("j_max_bytes", 2 * 1024 ** 3)
        top = int(self._threshold("j_top_n", 8))
        return self._pg(SQL("""
            SELECT c.relname, pg_relation_size(c.oid) AS bytes
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
               AND pg_relation_size(c.oid) BETWEEN %s AND %s
             ORDER BY pg_relation_size(c.oid) DESC
             LIMIT %s
        """, floor, ceiling, top))

    def _detect_j1_table_bloat(self):
        if not self._has_extension("pgstattuple"):
            return [{
                "target_kind": "config",
                "target_ref": "pgstattuple",
                "severity": "info",
                "metric": _("extension not installed"),
                "threshold": _("needed for measured (rather than estimated) bloat"),
                "recommendation": _(
                    "CREATE EXTENSION pgstattuple; gives an exact dead-tuple and free-space "
                    "percentage per table. Without it this module falls back to the dead-row "
                    "ratio from pg_stat_user_tables, which under-reports bloat after a VACUUM."),
                "fix_kind": "config",
            }]
        pct = self._threshold("j1_pct", 25.0)
        out = []
        for cand in self._bloat_candidates():
            try:
                with self.env.cr.savepoint():
                    rows = self._pg(SQL(
                        "SELECT dead_tuple_percent, free_percent FROM pgstattuple(%s)",
                        cand["relname"]))
            except Exception:
                continue
            if not rows:
                continue
            dead = float(rows[0].get("dead_tuple_percent") or 0)
            free = float(rows[0].get("free_percent") or 0)
            if dead + free < pct:
                continue
            out.append({
                "target_kind": "table",
                "target_ref": cand["relname"],
                "severity": "critical" if dead + free >= 50 else "warning",
                "metric": _("%(d).1f%% dead + %(f).1f%% free of %(size)s") % {
                    "d": dead, "f": free, "size": _human(cand["bytes"])},
                "threshold": _("more than %.0f%% of the table is dead or empty space") % pct,
                "recommendation": _(
                    "Measured with pgstattuple, not estimated. A plain VACUUM reclaims the dead "
                    "tuples for reuse; only VACUUM FULL or pg_repack returns the space to the disk, "
                    "and VACUUM FULL takes an exclusive lock — schedule it."),
                "fix_kind": "vacuum",
                "fix_sql": 'VACUUM (ANALYZE) "%s"' % cand["relname"],
            })
        return out

    def _detect_j2_index_bloat(self):
        if not self._has_extension("pgstattuple"):
            return []
        density_floor = self._threshold("j2_density", 60.0)
        min_bytes = self._threshold("j2_min_bytes", 8 * 1024 * 1024)
        idx = self._pg(SQL("""
            SELECT i.indexrelid::regclass::text AS idxname,
                   i.indrelid::regclass::text  AS tblname,
                   pg_relation_size(i.indexrelid) AS bytes
              FROM pg_index i
              JOIN pg_class c ON c.oid = i.indexrelid
              JOIN pg_am am ON am.oid = c.relam
             WHERE am.amname = 'btree'
               AND pg_relation_size(i.indexrelid) > %s
             ORDER BY pg_relation_size(i.indexrelid) DESC
             LIMIT 12
        """, min_bytes))
        out = []
        for r in idx:
            try:
                with self.env.cr.savepoint():
                    rows = self._pg(SQL(
                        "SELECT avg_leaf_density FROM pgstatindex(%s)", r["idxname"]))
            except Exception:
                continue
            if not rows:
                continue
            density = float(rows[0].get("avg_leaf_density") or 100)
            if density >= density_floor:
                continue
            out.append({
                "target_kind": "index",
                "target_ref": r["idxname"],
                "metric": _("%(d).1f%% leaf density, %(size)s on disk") % {
                    "d": density, "size": _human(r["bytes"])},
                "threshold": _("leaf density below %.0f%%") % density_floor,
                "recommendation": _(
                    "This index is mostly empty pages, so every lookup reads more blocks than it "
                    "needs to. REINDEX INDEX CONCURRENTLY \"%s\"; rebuilds it without blocking "
                    "writes.") % r["idxname"],
                "fix_kind": "none",
                "evidence": 'REINDEX INDEX CONCURRENTLY "%s";' % r["idxname"],
            })
        return out

    # ------------------------------------------------------------------
    # Planner validation of every index we recommend
    # ------------------------------------------------------------------
    def _post_detect(self, ran_codes):
        super()._post_detect(ran_codes)
        try:
            self._validate_index_recommendations()
        except Exception:
            _logger.exception("mdx_perf_auditor: index validation pass failed")

    def _validate_index_recommendations(self):
        if not self._has_extension("hypopg"):
            return
        findings = self.env["perf.audit.finding"].sudo().search([
            ("run_id", "=", self.id),
            ("fix_kind", "=", "create_index"),
            ("status", "in", ("open", "acknowledged")),
        ], limit=60)
        for f in findings:
            verdict = self._planner_would_use(f.fix_sql or "")
            if verdict is None:
                continue
            note = (_("Planner check: this index IS used for a representative lookup.")
                    if verdict else
                    _("Planner check: the planner would NOT use this index for a representative "
                      "lookup — the column is probably low-cardinality or the table is small "
                      "enough to scan. Safe to skip."))
            vals = {
                "planner_verdict": "used" if verdict else "unused",
                "planner_note": note,
                "evidence": "\n\n".join(filter(None, [f.evidence, note])),
            }
            if not verdict:
                vals["severity"] = "info"
            f.write(vals)

    def _planner_would_use(self, fix_sql):
        """True / False / None (couldn't test) — ask the planner about a
        hypothetical index without building anything."""
        m = _IX_TARGET.search(fix_sql or "")
        if not m:
            return None
        table = m.group(1)
        cols = _IX_COL.findall(m.group(2))
        if not table or not cols:
            return None
        col = cols[0]
        try:
            with self.env.cr.savepoint():
                probe = self._pg(SQL(
                    'SELECT %s AS v FROM %s WHERE %s IS NOT NULL LIMIT 1',
                    SQL.identifier(col), SQL.identifier(table), SQL.identifier(col)))
                if not probe:
                    return None
                value = probe[0]["v"]

                self.env.cr.execute(SQL("SELECT hypopg_reset()"))
                ddl = 'CREATE INDEX ON "%s" (%s)' % (
                    table, ", ".join('"%s"' % c for c in cols[:2]))
                created = self._pg(SQL("SELECT indexname FROM hypopg_create_index(%s)", ddl))
                if not created:
                    return None
                hypo_name = created[0]["indexname"]

                self.env.cr.execute(SQL(
                    "EXPLAIN (FORMAT JSON) SELECT 1 FROM %s WHERE %s = %s",
                    SQL.identifier(table), SQL.identifier(col), value))
                row = self.env.cr.fetchone()
                plan = row[0] if row else None
                if isinstance(plan, str):
                    plan = json.loads(plan)
                used = hypo_name in json.dumps(plan or [])
                self.env.cr.execute(SQL("SELECT hypopg_reset()"))
            return used
        except Exception as exc:
            _logger.debug("mdx_perf_auditor: hypopg probe failed (%s)", exc)
            return None


def _human(n):
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return "%.0f %s" % (n, unit)
        n /= 1024
    return "%.1f PB" % n
