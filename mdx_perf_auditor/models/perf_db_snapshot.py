import datetime
import logging

from odoo import _, api, fields, models
from odoo.tools import SQL

_logger = logging.getLogger(__name__)


class PerfDbSnapshot(models.Model):
    _name = "perf.db.snapshot"
    _description = "PostgreSQL Statistics Snapshot"
    _order = "date desc, id desc"
    _log_access = False  # high-write, history-only

    date = fields.Datetime(default=fields.Datetime.now, index=True)
    db_size_bytes = fields.Float(aggregator="max")
    db_size_human = fields.Char()
    dead_tuples_total = fields.Float(aggregator="max")
    live_tuples_total = fields.Float(aggregator="max")
    seq_scans_total = fields.Float()
    idx_scans_total = fields.Float()
    table_json = fields.Json("Top tables")

    @api.model
    def cron_snapshot(self):
        keep_days = int(self.env["ir.config_parameter"].sudo().get_param(
            "mdx_perf_auditor.snapshot_retention_days", "90"
        ))
        cr = self.env.cr

        cr.execute(SQL("SELECT pg_database_size(current_database())"))
        size = cr.fetchone()[0]
        cr.execute(SQL("SELECT pg_size_pretty(pg_database_size(current_database()))"))
        size_h = cr.fetchone()[0]

        cr.execute(SQL("""
            SELECT COALESCE(SUM(n_dead_tup), 0), COALESCE(SUM(n_live_tup), 0),
                   COALESCE(SUM(seq_scan), 0), COALESCE(SUM(idx_scan), 0)
              FROM pg_stat_user_tables
        """))
        dead, live, seq, idx = cr.fetchone()

        cr.execute(SQL("""
            SELECT c.relname AS name,
                   pg_total_relation_size(c.oid) AS bytes,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS human,
                   c.reltuples::bigint AS est_rows
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
             ORDER BY pg_total_relation_size(c.oid) DESC
             LIMIT 25
        """))
        tables = [
            {"name": r[0], "bytes": int(r[1] or 0), "human": r[2], "rows": int(r[3] or 0)}
            for r in cr.fetchall()
        ]

        self.create({
            "db_size_bytes": size,
            "db_size_human": size_h,
            "dead_tuples_total": dead,
            "live_tuples_total": live,
            "seq_scans_total": seq,
            "idx_scans_total": idx,
            "table_json": tables,
        })

        cutoff = fields.Datetime.now() - datetime.timedelta(days=keep_days)
        self.sudo().search([("date", "<", cutoff)]).unlink()
