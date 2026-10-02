"""Group C — Runtime profiling. Reads the profiling sessions Odoo already stores
in ir.profile (Settings must have enabled profiling for data to exist) and, when
the extension is present, pg_stat_statements. Nothing here enables profiling by
itself — that stays an explicit admin choice."""

import json
import re
from collections import defaultdict

from odoo import _, models
from odoo.tools import SQL

_WS = re.compile(r"\s+")
_NUM = re.compile(r"\b\d+\b")


def _norm(query):
    q = _WS.sub(" ", (query or "").strip())
    q = _NUM.sub("?", q)
    return q[:400]


class PerfAuditRunRuntime(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "C0", "group": "runtime", "method": "_detect_c0_no_data",
             "title": _("No profiling data to analyse"), "severity": "info"},
            {"code": "C1", "group": "runtime", "method": "_detect_c1_slow_endpoints",
             "title": _("Slow request"), "severity": "warning"},
            {"code": "C3", "group": "runtime", "method": "_detect_c3_nplus1",
             "title": _("N+1 query pattern"), "severity": "critical"},
            {"code": "C6", "group": "runtime", "method": "_detect_c6_slow_sql",
             "title": _("Slow SQL statement"), "severity": "warning"},
        ]

    def _profiles(self, limit=500):
        return self.env["ir.profile"].sudo().search([], limit=limit)

    def _has_pg_stat_statements(self):
        return bool(self._pg(SQL(
            "SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'"
        )))

    # ------------------------------------------------------------------
    def _detect_c0_no_data(self):
        if self._profiles(limit=1) or self._has_pg_stat_statements():
            return []
        return [{
            "target_kind": "config",
            "target_ref": "ir.profile",
            "metric": _("0 stored profiling sessions, pg_stat_statements not installed"),
            "threshold": _("runtime findings need at least one data source"),
            "recommendation": _(
                "Groups C is idle. Enable profiling for an hour during real usage "
                "(Settings ▸ Technical ▸ Enable profiling), or install the pg_stat_statements "
                "extension, then re-run the audit."),
            "fix_kind": "none",
        }]

    def _detect_c1_slow_endpoints(self):
        threshold = self._threshold("c1_seconds", 2.0)
        agg = defaultdict(lambda: {"n": 0, "total": 0.0, "max": 0.0, "sql": 0})
        for p in self._profiles():
            key = (p.name or "unnamed").split("?")[0][:120]
            a = agg[key]
            a["n"] += 1
            a["total"] += p.duration or 0.0
            a["max"] = max(a["max"], p.duration or 0.0)
            a["sql"] += p.sql_count or 0
        out = []
        for key, a in agg.items():
            avg = a["total"] / a["n"]
            if avg < threshold and a["max"] < threshold * 2:
                continue
            out.append({
                "target_kind": "other",
                "target_ref": key,
                "severity": "critical" if avg >= threshold * 2 else "warning",
                "metric": _("avg %.2fs / max %.2fs over %d samples, ~%d queries each") % (
                    avg, a["max"], a["n"], a["sql"] / a["n"]),
                "threshold": _("average request time above %.1fs") % threshold,
                "recommendation": _(
                    "Profile this route directly (Speedscope view on the matching ir.profile rows) "
                    "to see whether the time is in SQL or in Python."),
                "fix_kind": "none",
            })
        return out

    def _detect_c3_nplus1(self):
        repeat = int(self._threshold("c3_repeat", 20))
        out = []
        for p in self._profiles(limit=300):
            if not p.sql:
                continue
            try:
                entries = json.loads(p.sql)
            except (ValueError, TypeError):
                continue
            counter = defaultdict(lambda: [0, 0.0])
            for e in entries:
                n = _norm(e.get("query"))
                counter[n][0] += 1
                counter[n][1] += e.get("time", 0.0)
            for q, (count, total) in counter.items():
                if count < repeat:
                    continue
                out.append({
                    "target_kind": "other",
                    "target_ref": "%s :: %s" % ((p.name or "?")[:40], q[:90]),
                    "metric": _("%d executions of one query in a single request, %.0f ms total") % (
                        count, total * 1000),
                    "threshold": _("same query run more than %d times per request") % repeat,
                    "recommendation": _(
                        "Classic N+1. Prefetch the relation, switch the loop to read_group, or move "
                        "the work into a batched compute. Query:\n%s") % q,
                    "fix_kind": "none",
                })
        return out

    def _detect_c6_slow_sql(self):
        ms = self._threshold("c6_ms", 200.0)
        if self._has_pg_stat_statements():
            rows = self._pg(SQL("""
                SELECT query, calls, mean_exec_time, max_exec_time
                  FROM pg_stat_statements
                 WHERE mean_exec_time > %s
                 ORDER BY mean_exec_time DESC
                 LIMIT 20
            """, ms))
            return [{
                "target_kind": "other",
                "target_ref": _norm(r["query"])[:120],
                "metric": self.env._("mean %.0f ms / max %.0f ms over %s calls") % (
                    r["mean_exec_time"], r["max_exec_time"], "{:,}".format(int(r["calls"]))),
                "threshold": self.env._("mean execution time above %.0f ms") % ms,
                "recommendation": self.env._(
                    "Run EXPLAIN (ANALYZE, BUFFERS) on this statement. A Seq Scan means a missing "
                    "index; a large Sort means work_mem is too small."),
                "fix_kind": "none",
            } for r in rows]

        # fall back to the profiler traces
        worst = defaultdict(lambda: [0, 0.0, 0.0])
        for p in self._profiles(limit=300):
            if not p.sql:
                continue
            try:
                entries = json.loads(p.sql)
            except (ValueError, TypeError):
                continue
            for e in entries:
                t = e.get("time", 0.0)
                if t * 1000 < ms:
                    continue
                w = worst[_norm(e.get("query"))]
                w[0] += 1
                w[1] += t
                w[2] = max(w[2], t)
        return [{
            "target_kind": "other",
            "target_ref": q[:120],
            "metric": self.env._("seen %d times, up to %.0f ms") % (c, mx * 1000),
            "threshold": self.env._("single statement over %.0f ms") % ms,
            "recommendation": self.env._("Run EXPLAIN (ANALYZE, BUFFERS) on this statement and index accordingly."),
            "fix_kind": "none",
        } for q, (c, _tot, mx) in sorted(worst.items(), key=lambda kv: kv[1][2], reverse=True)[:20]]
