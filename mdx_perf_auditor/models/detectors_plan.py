"""Group G — Query plan analysis (deep).

Takes the slow statements Odoo's profiler captured (with their real parameters),
runs EXPLAIN (ANALYZE, BUFFERS) on the read-only ones inside a savepoint, and
reads the resulting plan tree for the four things that actually explain a slow
Odoo query: a sequential scan that should be an index scan, a planner estimate
that is wildly wrong, a sort that spilled to disk, and a nested loop hammering an
unindexed inner relation.
"""

import json
import logging
import re
from collections import defaultdict

from odoo import _, models
from odoo.tools import SQL

_logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_READ_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_WRITES = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|DROP|ALTER|GRANT|COPY)\b",
                     re.IGNORECASE)
_FILTER_COL = re.compile(r"\(?([a-z_][a-z0-9_]*)\s*(?:=|@>|&&|IN|>=|<=|>|<)", re.IGNORECASE)
_EXPLAIN_TIMEOUT_MS = 5000
_PLAN_CACHE = {}  # run id -> [(stat, plan)], one EXPLAIN pass shared by G1-G4
_COL_CACHE = {}   # run id -> {"cols": {rel: set}, "idx": {rel: set}}

# The auditor profiles itself if it runs during a capture window. Its own
# catalog queries are not application problems, so drop them.
_SELF_QUERIES = re.compile(
    r"\b(pg_stat_user_\w+|pg_statio_\w+|pg_stat_statements|pg_class|pg_index|pg_constraint|"
    r"pg_settings|pg_extension|hypopg\w*|pgstat\w+|perf_audit_\w+|perf_db_snapshot|"
    r"perf_cron_run|ir_profile)\b",
    re.IGNORECASE,
)


def _norm(q):
    return _WS.sub(" ", (q or "").strip())


class PerfAuditRunPlan(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "G0", "group": "runtime", "depth": "deep",
             "method": "_detect_g0_no_capture",
             "title": _("No captured traffic to analyse"), "severity": "info"},
            {"code": "G1", "group": "db", "depth": "deep",
             "method": "_detect_g1_seq_scan",
             "title": _("Sequential scan in a slow query"), "severity": "critical"},
            {"code": "G2", "group": "db", "depth": "deep",
             "method": "_detect_g2_bad_estimate",
             "title": _("Planner row estimate is wrong"), "severity": "warning"},
            {"code": "G3", "group": "config", "depth": "deep",
             "method": "_detect_g3_disk_sort",
             "title": _("Sort spilled to disk"), "severity": "warning"},
            {"code": "G4", "group": "db", "depth": "deep",
             "method": "_detect_g4_nested_loop",
             "title": _("Nested loop over an unindexed relation"), "severity": "warning"},
        ]

    # ------------------------------------------------------------------
    # Plan collection — cached on the run for the length of one pass
    # ------------------------------------------------------------------
    def _slow_statements(self):
        """Slowest distinct read-only statements from the captured profiles."""
        min_ms = self._threshold("g_min_ms", 120.0)
        top_n = int(self._threshold("g_top_n", 12))
        profiles = self.env["perf.capture"].captured_profiles(limit=150)
        best = {}
        for p in profiles:
            if not p.sql:
                continue
            try:
                entries = json.loads(p.sql)
            except (ValueError, TypeError):
                continue
            for e in entries:
                t = (e.get("time") or 0.0) * 1000.0
                if t < min_ms:
                    continue
                full = e.get("full_query") or e.get("query") or ""
                if not _READ_ONLY.match(full):
                    continue
                if _WRITES.search(full[6:]):  # writing CTE, or DDL — never explain it
                    continue
                if _SELF_QUERIES.search(full):  # the auditor profiling itself
                    continue
                key = _norm(e.get("query") or full)[:300]
                if key not in best or t > best[key]["ms"]:
                    best[key] = {"ms": t, "sql": full, "route": p.name or ""}
        return sorted(best.values(), key=lambda r: r["ms"], reverse=True)[:top_n]

    def _explain(self, statement):
        """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) inside a savepoint. Returns the
        root plan dict, or None when the statement cannot be explained safely.

        The statement already ran against this database with these exact
        parameters, and we refuse anything that is not a pure read, so replaying
        it under EXPLAIN ANALYZE has no side effects. The savepoint and the
        statement timeout are belt and braces."""
        # Odoo's SQL wrapper treats % as a placeholder marker; profiler queries
        # can legitimately contain one inside a LIKE pattern.
        raw = statement.replace("%", "%%")
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    SQL("SET LOCAL statement_timeout TO %s" % int(_EXPLAIN_TIMEOUT_MS)))
                self.env.cr.execute(
                    SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) %s", SQL(raw)))
                row = self.env.cr.fetchone()
            if not row:
                return None
            data = row[0]
            if isinstance(data, str):
                data = json.loads(data)
            return data[0]["Plan"] if data else None
        except Exception as exc:
            _logger.debug("mdx_perf_auditor: EXPLAIN failed (%s)", exc)
            return None
        finally:
            # SET LOCAL is scoped to the transaction, not to the savepoint:
            # releasing the savepoint after a successful EXPLAIN leaves the 5 s
            # cap in force for the rest of the audit, which would abort the long
            # pgstattuple scans in J1/J2 on any database big enough to need them.
            # (A failed EXPLAIN rolls the savepoint back, which already undoes it.)
            try:
                self.env.cr.execute(SQL("RESET statement_timeout"))
            except Exception:  # pragma: no cover - transaction already aborted
                pass

    def _plans(self):
        """[(stat, plan)] for every slow statement we could explain.

        Cached per run so the four G detectors explain each statement once."""
        cached = _PLAN_CACHE.get(self.id)
        if cached is not None:
            return cached
        out = []
        for stat in self._slow_statements():
            plan = self._explain(stat["sql"])
            if plan:
                out.append((stat, plan))
        if len(_PLAN_CACHE) > 8:
            _PLAN_CACHE.clear()
            _COL_CACHE.clear()
        _PLAN_CACHE[self.id] = out
        return out

    @staticmethod
    def _walk(node, depth=0):
        yield node, depth
        for child in node.get("Plans", []) or []:
            yield from PerfAuditRunPlan._walk(child, depth + 1)

    @staticmethod
    def _evidence(stat, node):
        return "-- %s ms · %s\n%s\n\n%s" % (
            round(stat["ms"], 1), stat["route"][:80], stat["sql"][:900],
            json.dumps({k: v for k, v in node.items() if k != "Plans"}, indent=2)[:900],
        )

    # ------------------------------------------------------------------
    def _detect_g0_no_capture(self):
        status = self.env["perf.capture"].status()
        if status["profiles"]:
            return []
        return [{
            "target_kind": "config",
            "target_ref": "capture",
            "metric": _("0 profiles recorded"),
            "threshold": _("deep query analysis needs captured traffic"),
            "recommendation": _(
                "Start a capture from the dashboard, use the parts of Odoo that feel slow for a few "
                "minutes, then run the deep analysis again. Group G explains real statements with "
                "their real parameters — it has nothing to work with until then."),
            "fix_kind": "none",
        }]

    def _col_cache(self):
        """Per-run catalogue lookups, so G1 hits pg_attribute once per table."""
        if len(_COL_CACHE) > 8:
            _COL_CACHE.clear()
        return _COL_CACHE.setdefault(self.id, {})

    def _real_columns(self, relname):
        """Column names that actually exist on a relation, cached per run."""
        cache = self._col_cache().setdefault("cols", {})
        if relname not in cache:
            cache[relname] = {r["attname"] for r in self._pg(SQL("""
                SELECT a.attname
                  FROM pg_attribute a
                  JOIN pg_class c ON c.oid = a.attrelid
                 WHERE c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
            """, relname))}
        return cache[relname]

    def _leading_index_cols(self, relname):
        """Columns already served as the first key of an index on this relation.

        An index on (a, b) answers a filter on ``a``; it does not help a filter
        on ``b`` alone, so only the leading column counts."""
        cache = self._col_cache().setdefault("idx", {})
        if relname not in cache:
            cache[relname] = {r["attname"] for r in self._pg(SQL("""
                SELECT a.attname
                  FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indrelid
                  JOIN pg_attribute a
                    ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                 WHERE c.relname = %s
            """, relname))}
        return cache[relname]

    def _detect_g1_seq_scan(self):
        min_rows = self._threshold("g1_min_rows", 5000)
        out = []
        for stat, plan in self._plans():
            for node, _d in self._walk(plan):
                if node.get("Node Type") != "Seq Scan":
                    continue
                rel = node.get("Relation Name")
                scanned = (node.get("Actual Rows") or 0) + (node.get("Rows Removed by Filter") or 0)
                if not rel or scanned < min_rows:
                    continue
                cols = []
                for m in _FILTER_COL.finditer(node.get("Filter") or ""):
                    c = m.group(1)
                    if c not in cols and c not in ("true", "false", "null"):
                        cols.append(c)
                col_txt = ", ".join(cols[:3]) if cols else _("the filtered column")
                # Only recommend an index for columns that really exist on this
                # relation and are not already the leading key of one. Without
                # this the detector happily suggests an index on the primary key.
                cols = [c for c in cols if c in self._real_columns(rel)
                        and c not in self._leading_index_cols(rel)]
                fix_sql = ""
                if cols:
                    name = ("ix_%s_%s" % (rel, "_".join(cols[:2])))[:63]
                    fix_sql = 'CREATE INDEX CONCURRENTLY IF NOT EXISTS "%s" ON "%s" (%s)' % (
                        name, rel, ", ".join('"%s"' % c for c in cols[:2]))
                out.append({
                    "target_kind": "table",
                    "target_ref": "%s (%s)" % (rel, col_txt),
                    "metric": _("%(rows)s rows scanned, %(ms)s ms statement") % {
                        "rows": "{:,}".format(int(scanned)), "ms": round(stat["ms"], 1)},
                    "threshold": _("sequential scan reading more than %s rows")
                    % "{:,}".format(int(min_rows)),
                    "recommendation": _(
                        "A live query reads %(rel)s end to end and throws most rows away. An index on "
                        "%(cols)s turns this into an index scan. Confirmed against the real plan, not "
                        "guessed from the schema.") % {"rel": rel, "cols": col_txt},
                    "fix_kind": "create_index" if fix_sql else "none",
                    "fix_sql": fix_sql,
                    "evidence": self._evidence(stat, node),
                })
        return out

    def _detect_g2_bad_estimate(self):
        factor = self._threshold("g2_factor", 10.0)
        min_actual = self._threshold("g2_min_rows", 1000)
        seen = set()
        out = []
        for stat, plan in self._plans():
            for node, _d in self._walk(plan):
                rel = node.get("Relation Name")
                est = float(node.get("Plan Rows") or 0)
                act = float(node.get("Actual Rows") or 0) * float(node.get("Actual Loops") or 1)
                if not rel or act < min_actual or est <= 0:
                    continue
                ratio = max(est, act) / max(min(est, act), 1.0)
                if ratio < factor or rel in seen:
                    continue
                seen.add(rel)
                out.append({
                    "target_kind": "table",
                    "target_ref": rel,
                    "metric": _("planner expected %(est)s rows, got %(act)s (%(r)sx off)") % {
                        "est": "{:,}".format(int(est)), "act": "{:,}".format(int(act)),
                        "r": int(ratio)},
                    "threshold": _("estimate off by more than %dx") % int(factor),
                    "recommendation": _(
                        "With estimates this wrong the planner picks the wrong join order and the "
                        "wrong scan type. Run ANALYZE on %s; if it comes back, raise the column "
                        "statistics target or lower the autovacuum analyze scale factor.") % rel,
                    "fix_kind": "analyze",
                    "fix_sql": 'ANALYZE "%s"' % rel,
                    "evidence": self._evidence(stat, node),
                })
        return out

    def _detect_g3_disk_sort(self):
        out = []
        seen = set()
        for stat, plan in self._plans():
            for node, _d in self._walk(plan):
                space = node.get("Sort Space Type") or ""
                if space.lower() != "disk":
                    continue
                used = node.get("Sort Space Used") or 0
                key = (stat["route"][:40], node.get("Node Type"))
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "target_kind": "config",
                    "target_ref": "work_mem · %s" % (stat["route"][:60] or node.get("Node Type")),
                    "metric": _("%(kb)s kB sorted on disk in a %(ms)s ms statement") % {
                        "kb": "{:,}".format(int(used)), "ms": round(stat["ms"], 1)},
                    "threshold": _("sorts should fit in work_mem"),
                    "recommendation": _(
                        "This sort exceeded work_mem and went to disk. Raise work_mem to at least "
                        "%d MB (it is per sort operation, so multiply by concurrent queries before "
                        "you set it), or reduce the rows being sorted.")
                    % max(8, int(used / 1024 * 2)),
                    "fix_kind": "config",
                    "evidence": self._evidence(stat, node),
                })
        return out

    def _detect_g4_nested_loop(self):
        min_loops = self._threshold("g4_min_loops", 500)
        out = []
        seen = set()
        for stat, plan in self._plans():
            for node, _d in self._walk(plan):
                loops = float(node.get("Actual Loops") or 1)
                ntype = node.get("Node Type") or ""
                rel = node.get("Relation Name")
                if loops < min_loops or not rel:
                    continue
                if ntype not in ("Seq Scan", "Index Scan", "Bitmap Heap Scan"):
                    continue
                if ntype != "Seq Scan" and loops < min_loops * 4:
                    continue
                key = (rel, ntype)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "target_kind": "table",
                    "target_ref": "%s (%s x%d)" % (rel, ntype, int(loops)),
                    "metric": _("inner side executed %(n)s times, %(ms)s ms total") % {
                        "n": "{:,}".format(int(loops)), "ms": round(stat["ms"], 1)},
                    "threshold": _("inner relation re-scanned more than %s times")
                    % "{:,}".format(int(min_loops)),
                    "recommendation": _(
                        "The planner chose a nested loop and re-reads %s for every outer row. Index "
                        "the join column so each lookup is cheap, or fix the row estimate that made "
                        "the loop look attractive (see the estimate findings).") % rel,
                    "fix_kind": "none",
                    "evidence": self._evidence(stat, node),
                })
        return out
