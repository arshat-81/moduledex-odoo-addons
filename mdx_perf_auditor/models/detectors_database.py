"""Group A — Database health. Read-only queries against the PostgreSQL catalog
and the cumulative statistics views. Nothing here writes."""

import hashlib

from odoo import _, models
from odoo.tools import SQL

_A2_INDEX_CACHE = {}  # run id -> {table: [[col, ...], ...]}

# Tables Odoo (or a common add-on) is known to let grow unbounded, with the
# lever that keeps them in check. Strings are translated at use site.
KNOWN_BLOAT = [
    ("mail_message", "Discuss messages",
     "Add a retention rule under Settings > Technical > Email > Messages, or install a message-GC add-on."),
    ("mail_tracking_value", "Field-change tracking rows",
     "These follow mail.message - pruning old messages clears them."),
    ("mail_notification", "Notification rows",
     "Core prunes these after 6 months; check the 'Delete Notifications' cron is active."),
    ("bus_bus", "Bus / long-polling backlog",
     "Core GC keeps only recent rows; a large table means the autovacuum cron is not keeping up."),
    ("ir_logging", "Application log rows",
     "Raise log_level away from debug and add a retention cron on ir.logging."),
    ("ir_attachment", "Attachments (metadata rows)",
     "Run the Attachments checks; move the filestore off the database if db_datas is used."),
    ("base_import_import", "Import wizard scratch rows",
     "Safe to clear; core does not always GC these."),
    ("ir_profile", "Stored profiling sessions",
     "Core keeps 30 days. A large table means profiling was left enabled - see F5."),
    ("auditlog_log_line", "OCA audit-log lines",
     "The OCA auditlog table needs an index on log_id and aggressive retention; it is a known slow-down."),
]


def _ix_name(table, col):
    base = "ix_%s_%s" % (table, col)
    if len(base) <= 63:
        return base
    return "ix_%s_%s" % (table[:20], hashlib.md5(base.encode()).hexdigest()[:12])


class PerfAuditRunDatabase(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "A1", "group": "db", "method": "_detect_a1_unindexed_fk",
             "title": _("Unindexed foreign key"), "severity": "warning"},
            {"code": "A2", "group": "db", "method": "_detect_a2_unindexed_order",
             "title": _("Sort column is not indexed"), "severity": "warning"},
            {"code": "A3", "group": "db", "method": "_detect_a3_dead_tuples",
             "title": _("Table bloat / dead rows"), "severity": "warning"},
            {"code": "A4", "group": "db", "method": "_detect_a4_stale_stats",
             "title": _("Stale planner statistics"), "severity": "warning"},
            {"code": "A5", "group": "db", "method": "_detect_a5_unused_index",
             "title": _("Unused index"), "severity": "info"},
            {"code": "A6", "group": "db", "method": "_detect_a6_seq_scans",
             "title": _("Sequential-scan-heavy table"), "severity": "warning"},
            {"code": "A7", "group": "db", "method": "_detect_a7_huge_tables",
             "title": _("Very large table"), "severity": "info"},
            {"code": "A8", "group": "db", "method": "_detect_a8_cache_hit",
             "title": _("Low cache-hit ratio"), "severity": "warning"},
            {"code": "A9", "group": "db", "method": "_detect_a9_known_bloat",
             "title": _("Table growing unbounded"), "severity": "warning"},
        ]

    # ------------------------------------------------------------------
    def _detect_a1_unindexed_fk(self):
        # Skip tiny tables entirely, and hold audit-user columns to a much higher
        # bar - create_uid/write_uid only bite on very large tables at user deletion.
        floor = self._threshold("a1_min_table_bytes", 4 * 1024 * 1024)
        audit_floor = self._threshold("a1_audit_col_bytes", 64 * 1024 * 1024)
        big = self._threshold("a1_big_table_bytes", 100 * 1024 * 1024)
        audit_cols = ("create_uid", "write_uid")
        rows = self._pg(SQL("""
            SELECT c.conrelid::regclass::text AS table_name,
                   a.attname                  AS column_name,
                   pg_relation_size(c.conrelid) AS table_bytes,
                   pg_size_pretty(pg_relation_size(c.conrelid)) AS table_size
              FROM pg_constraint c
              JOIN pg_attribute a
                ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
             WHERE c.contype = 'f'
               AND array_length(c.conkey, 1) = 1
               AND pg_relation_size(c.conrelid) >= %s
               AND NOT EXISTS (
                     SELECT 1 FROM pg_index i
                      WHERE i.indrelid = c.conrelid AND i.indkey[0] = a.attnum)
             ORDER BY pg_relation_size(c.conrelid) DESC
        """, floor))
        out = []
        for r in rows:
            table = r["table_name"].strip('"')
            col = r["column_name"]
            tb = r["table_bytes"] or 0
            if col in audit_cols and tb < audit_floor:
                continue
            ix = _ix_name(table, col)
            out.append({
                "target_kind": "field",
                "target_ref": "%s.%s" % (table, col),
                "severity": "critical" if tb >= big else "warning",
                "metric": _("%s table, no index on the FK column") % (r["table_size"] or "0 bytes"),
                "threshold": _("foreign-key columns on tables over %s should be indexed")
                % _human_bytes(floor),
                "recommendation": _(
                    "Joins and on-delete checks against this column fall back to a sequential scan. "
                    "Create a btree index."),
                "fix_kind": "create_index",
                "fix_sql": 'CREATE INDEX CONCURRENTLY IF NOT EXISTS "%s" ON "%s" ("%s")' % (ix, table, col),
            })
        return out

    def _index_columns_by_table(self):
        """{table: [[col, ...], ...]} - the ordered key columns of every index.

        One catalogue sweep for the whole run: A2 asks about hundreds of models
        and a query per table would cost more than the audit it is doing.
        Expression indexes have attnum 0 and simply do not appear, which is
        right - we cannot match an expression back to a column name."""
        cached = _A2_INDEX_CACHE.get(self.id)
        if cached is None:
            by_index = {}
            for r in self._pg(SQL("""
                SELECT c.relname AS table_name, i.indexrelid, k.ord, a.attname
                  FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a
                    ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                 WHERE n.nspname = 'public' AND NOT a.attisdropped
                 ORDER BY c.relname, i.indexrelid, k.ord
            """)):
                by_index.setdefault((r["table_name"], r["indexrelid"]), []).append(r["attname"])
            cached = {}
            for (table, _oid), cols in by_index.items():
                cached.setdefault(table, []).append(cols)
            if len(_A2_INDEX_CACHE) > 8:
                _A2_INDEX_CACHE.clear()
            _A2_INDEX_CACHE[self.id] = cached
        return cached

    def _order_columns_served(self, table, order_cols):
        """How many leading ORDER BY columns an existing index can satisfy.

        An index on (module, name) serves ``ORDER BY module, name`` completely,
        and ``ORDER BY module`` too - but nothing that starts with ``name``.
        Only a prefix match counts."""
        served = 0
        for cols in self._index_columns_by_table().get(table, []):
            n = 0
            while n < len(order_cols) and n < len(cols) and cols[n] == order_cols[n]:
                n += 1
            served = max(served, n)
        return served

    def _detect_a2_unindexed_order(self):
        min_rows = self._threshold("a2_min_rows", 20000)
        # Actual row counts, cheap.
        counts = {
            r["relname"]: r["n_live_tup"]
            for r in self._pg(SQL("SELECT relname, n_live_tup FROM pg_stat_user_tables"))
        }
        out = []
        for model_name, model in self.env.registry.items():
            if model._abstract or model._transient or not model._auto:
                continue
            table = model._table
            if counts.get(table, 0) < min_rows:
                continue
            order = self._model_attr(model_name, model, "_order", "id") or "id"
            order = order.replace(" desc", "").replace(" asc", "")
            order_cols = [p.strip() for p in order.split(",") if p.strip()]
            if not order_cols or order_cols == ["id"]:
                continue  # the primary key already answers this
            if any("." in p for p in order_cols):
                continue  # sorting through a relation - not an index question

            # A sort is served by ONE index whose leading columns match the
            # ORDER BY in the same sequence. ir_model_data sorts by
            # (module, model, name) and carries a unique index on
            # (module, name): asking each column whether it is individually
            # indexed reported three separate problems and offered three
            # single-column indexes, none of which would have helped.
            served = self._order_columns_served(table, order_cols)
            if served >= len(order_cols):
                continue

            cols = []
            for part in order_cols:
                if part == "id":
                    cols.append("id")
                    continue
                field = model._fields.get(part)
                if not field or not field.store or getattr(field, "column_type", None) is None:
                    cols = []
                    break
                cols.append(part)
            if not cols:
                continue

            col_list = ", ".join('"%s"' % c for c in cols)
            out.append({
                "target_kind": "table",
                "target_ref": "%s (%s)" % (table, ", ".join(cols)),
                "metric": _("%(rows)s rows, sorted by %(cols)s with no index to match") % {
                    "rows": "{:,}".format(int(counts[table])), "cols": ", ".join(cols)},
                "threshold": _("the default sort of a large model should be index-backed"),
                "recommendation": _(
                    "Every unfiltered list view of %(model)s pays this sort, and no existing index "
                    "leads with %(cols)s in that order. A single composite index covering the whole "
                    "sort is what helps here - indexing the columns separately does not.") % {
                        "model": model_name, "cols": ", ".join(cols)},
                "fix_kind": "create_index",
                "fix_sql": 'CREATE INDEX CONCURRENTLY IF NOT EXISTS "%s" ON "%s" (%s)' % (
                    _ix_name(table, "_".join(cols)), table, col_list),
            })
        return out

    def _detect_a3_dead_tuples(self):
        min_dead = self._threshold("a3_min_dead", 10000)
        ratio = self._threshold("a3_ratio", 0.2)
        rows = self._pg(SQL("""
            SELECT relname, n_live_tup, n_dead_tup,
                   CASE WHEN n_live_tup > 0
                        THEN round(n_dead_tup::numeric / n_live_tup, 3) ELSE NULL END AS r
              FROM pg_stat_user_tables
             WHERE n_dead_tup > %s
               AND n_dead_tup::numeric > n_live_tup * %s
             ORDER BY n_dead_tup DESC
        """, min_dead, ratio))
        return [{
            "target_kind": "table",
            "target_ref": r["relname"],
            "severity": "critical" if (r["r"] or 0) >= 0.5 else "warning",
            "metric": self.env._("%(dead)s dead rows vs %(live)s live (%(pct)s%%)") % {
                "dead": "{:,}".format(int(r["n_dead_tup"])),
                "live": "{:,}".format(int(r["n_live_tup"])),
                "pct": int((r["r"] or 0) * 100)},
            "threshold": self.env._("dead-row ratio above %d%%") % int(ratio * 100),
            "recommendation": self.env._(
                "Bloat slows every scan of this table. VACUUM now, then lower "
                "autovacuum_vacuum_scale_factor for this table."),
            "fix_kind": "vacuum",
            "fix_sql": 'VACUUM (ANALYZE) "%s"' % r["relname"],
        } for r in rows]

    def _detect_a4_stale_stats(self):
        min_rows = self._threshold("a4_min_rows", 50000)
        days = self._threshold("a4_days", 14)
        rows = self._pg(SQL("""
            SELECT relname, n_live_tup,
                   GREATEST(COALESCE(last_autoanalyze, 'epoch'::timestamptz),
                            COALESCE(last_analyze, 'epoch'::timestamptz)) AS last_any
              FROM pg_stat_user_tables
             WHERE n_live_tup > %s
               AND GREATEST(COALESCE(last_autoanalyze, 'epoch'::timestamptz),
                            COALESCE(last_analyze, 'epoch'::timestamptz))
                   < now() - (%s * interval '1 day')
             ORDER BY n_live_tup DESC
        """, min_rows, days))
        return [{
            "target_kind": "table",
            "target_ref": r["relname"],
            "metric": self.env._("%(rows)s rows, last analyzed %(when)s") % {
                "rows": "{:,}".format(int(r["n_live_tup"])),
                "when": (r["last_any"].date().isoformat()
                         if r["last_any"] and r["last_any"].year > 1970 else self.env._("never"))},
            "threshold": self.env._("statistics older than %d days on a large table") % int(days),
            "recommendation": self.env._(
                "The query planner is working from stale row estimates and may pick bad plans. "
                "Run ANALYZE and check the autovacuum daemon is running."),
            "fix_kind": "analyze",
            "fix_sql": 'ANALYZE "%s"' % r["relname"],
        } for r in rows]

    def _detect_a5_unused_index(self):
        min_bytes = self._threshold("a5_min_bytes", 1024 * 1024)
        rows = self._pg(SQL("""
            SELECT s.relname AS table_name, s.indexrelname AS index_name,
                   pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
              FROM pg_stat_user_indexes s
              JOIN pg_index i ON i.indexrelid = s.indexrelid
             WHERE s.idx_scan = 0
               AND NOT i.indisunique AND NOT i.indisprimary
               AND pg_relation_size(s.indexrelid) > %s
             ORDER BY pg_relation_size(s.indexrelid) DESC
        """, min_bytes))
        return [{
            "target_kind": "index",
            "target_ref": r["index_name"],
            "metric": self.env._("%(size)s, 0 scans since stats reset (on %(t)s)") % {
                "size": r["size"], "t": r["table_name"]},
            "threshold": self.env._("non-constraint index never used"),
            "recommendation": self.env._(
                "This index costs write time and disk on every change to %s and has never served a "
                "read. Confirm against a full traffic cycle, then drop it.") % r["table_name"],
            "fix_kind": "drop_index",
            "fix_sql": 'DROP INDEX CONCURRENTLY IF EXISTS "%s"' % r["index_name"],
        } for r in rows]

    def _detect_a6_seq_scans(self):
        min_rows = self._threshold("a6_min_rows", 20000)
        min_seq = self._threshold("a6_min_seq", 5000)
        mult = self._threshold("a6_mult", 4)
        rows = self._pg(SQL("""
            SELECT relname, seq_scan, COALESCE(idx_scan, 0) AS idx_scan, n_live_tup
              FROM pg_stat_user_tables
             WHERE n_live_tup > %s
               AND seq_scan > %s
               AND seq_scan > COALESCE(idx_scan, 0) * %s
             ORDER BY seq_scan DESC
        """, min_rows, min_seq, mult))
        return [{
            "target_kind": "table",
            "target_ref": r["relname"],
            "metric": self.env._("%(seq)s sequential scans vs %(idx)s index scans, %(rows)s rows") % {
                "seq": "{:,}".format(int(r["seq_scan"])),
                "idx": "{:,}".format(int(r["idx_scan"])),
                "rows": "{:,}".format(int(r["n_live_tup"]))},
            "threshold": self.env._("sequential scans dominate on a large table"),
            "recommendation": self.env._(
                "Something queries this table without hitting an index. Cross-check the unindexed "
                "foreign keys and sort columns above, then profile the offending request."),
            "fix_kind": "none",
        } for r in rows]

    def _detect_a7_huge_tables(self):
        limit_bytes = self._threshold("a7_bytes", 1024 ** 3)
        rows = self._pg(SQL("""
            SELECT c.relname,
                   pg_total_relation_size(c.oid) AS bytes,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
                   c.reltuples::bigint AS est_rows
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
               AND pg_total_relation_size(c.oid) > %s
             ORDER BY pg_total_relation_size(c.oid) DESC
        """, limit_bytes))
        return [{
            "target_kind": "table",
            "target_ref": r["relname"],
            "severity": "info",
            "metric": self.env._("%(size)s, ~%(rows)s rows") % {
                "size": r["size"], "rows": "{:,}".format(int(r["est_rows"]))},
            "threshold": self.env._("single table over %s") % _human_bytes(limit_bytes),
            "recommendation": self.env._(
                "Large tables are candidates for archival, partitioning by year, or moving history "
                "into a dedicated model. Not urgent on its own — context for the findings above."),
            "fix_kind": "none",
        } for r in rows]

    def _detect_a8_cache_hit(self):
        min_reads = self._threshold("a8_min_reads", 100000)
        ratio = self._threshold("a8_ratio", 0.9)
        rows = self._pg(SQL("""
            SELECT relname, heap_blks_hit, heap_blks_read,
                   round(heap_blks_hit::numeric
                         / NULLIF(heap_blks_hit + heap_blks_read, 0), 4) AS hit_ratio
              FROM pg_statio_user_tables
             WHERE heap_blks_read > %s
               AND heap_blks_hit::numeric
                   / NULLIF(heap_blks_hit + heap_blks_read, 0) < %s
             ORDER BY heap_blks_read DESC
        """, min_reads, ratio))
        return [{
            "target_kind": "table",
            "target_ref": r["relname"],
            "metric": self.env._("%(pct)s%% of reads served from cache") % {
                "pct": round((r["hit_ratio"] or 0) * 100, 1)},
            "threshold": self.env._("cache-hit ratio below %d%%") % int(ratio * 100),
            "recommendation": self.env._(
                "This table keeps going to disk. Raise shared_buffers, or the table is scanned in a "
                "way no index can help — check the scan pattern (A6)."),
            "fix_kind": "none",
        } for r in rows]

    def _detect_a9_known_bloat(self):
        min_rows = self._threshold("a9_min_rows", 500000)
        present = {
            r["relname"] for r in self._pg(SQL("SELECT relname FROM pg_stat_user_tables"))
        }
        out = []
        for table, label, hint in KNOWN_BLOAT:
            if table not in present:
                continue
            data = self._pg(SQL("""
                SELECT n_live_tup,
                       pg_size_pretty(pg_total_relation_size(relid)) AS size
                  FROM pg_stat_user_tables WHERE relname = %s
            """, table))
            if not data or (data[0]["n_live_tup"] or 0) < min_rows:
                continue
            out.append({
                "target_kind": "table",
                "target_ref": table,
                "name": _("%s is growing unbounded") % label,
                "metric": _("%(rows)s rows, %(size)s") % {
                    "rows": "{:,}".format(int(data[0]["n_live_tup"])), "size": data[0]["size"]},
                "threshold": _("over %s rows") % "{:,}".format(int(min_rows)),
                "recommendation": hint,
                # No safe fix: the finding is "this table has no retention
                # policy", and VACUUM does not give it one. Offering the button
                # would let someone run a long VACUUM, watch the finding come
                # straight back, and conclude the module is broken.
                "fix_kind": "none",
            })
        return out


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.0f %s" % (n, unit)
        n /= 1024
    return "%.0f PB" % n
