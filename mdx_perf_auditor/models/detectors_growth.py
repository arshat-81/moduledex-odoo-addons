"""Group H — Growth and capacity (deep).

Uses the PostgreSQL statistics snapshots the hourly cron has been collecting to
answer the questions a point-in-time scan cannot: what is growing, how fast, and
when does it become a problem.
"""

from odoo import _, fields, models


class PerfAuditRunGrowth(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "H0", "group": "db", "depth": "deep", "method": "_detect_h0_no_history",
             "title": _("Not enough history for trend analysis"), "severity": "info"},
            {"code": "H1", "group": "db", "depth": "deep", "method": "_detect_h1_growth",
             "title": _("Table growing fast"), "severity": "warning"},
            {"code": "H2", "group": "db", "depth": "deep", "method": "_detect_h2_vacuum_trend",
             "title": _("Autovacuum is falling behind"), "severity": "warning"},
        ]

    def _snapshot_pair(self):
        """(oldest, newest) snapshots spanning at least an hour, or (None, None)."""
        Snap = self.env["perf.db.snapshot"].sudo()
        newest = Snap.search([], limit=1, order="date desc")
        if not newest:
            return None, None
        min_hours = self._threshold("h_min_hours", 6)
        cutoff = fields.Datetime.subtract(newest.date, hours=min_hours)
        oldest = Snap.search([("date", "<=", cutoff)], limit=1, order="date asc")
        if not oldest or oldest.id == newest.id:
            # Extrapolating a daily rate from a few minutes of history produces
            # noise, so report "not enough history" instead of guessing.
            return None, None
        return oldest, newest

    def _detect_h0_no_history(self):
        oldest, newest = self._snapshot_pair()
        if oldest and newest:
            return []
        count = self.env["perf.db.snapshot"].sudo().search_count([])
        return [{
            "target_kind": "other",
            "target_ref": "perf.db.snapshot",
            "metric": _("%d snapshot(s) recorded") % count,
            "threshold": _("trend analysis needs two snapshots at least %d hours apart")
            % int(self._threshold("h_min_hours", 6)),
            "recommendation": _(
                "The hourly 'database statistics snapshot' cron builds this history. Leave it "
                "running for a day and the growth projections become available."),
            "fix_kind": "none",
        }]

    def _detect_h1_growth(self):
        oldest, newest = self._snapshot_pair()
        if not (oldest and newest):
            return []
        min_mb_per_day = self._threshold("h1_mb_per_day", 20.0)
        days = (newest.date - oldest.date).total_seconds() / 86400.0
        before = {t["name"]: t.get("bytes", 0) for t in (oldest.table_json or [])}
        out = []
        for t in (newest.table_json or []):
            was = before.get(t["name"])
            if was is None:
                continue
            delta = (t.get("bytes", 0) or 0) - was
            per_day = delta / days
            if per_day < min_mb_per_day * 1024 * 1024:
                continue
            projected = (t.get("bytes", 0) or 0) + per_day * 90
            out.append({
                "_per_day": per_day,
                "target_kind": "table",
                "target_ref": t["name"],
                "severity": "critical" if projected > 50 * 1024 ** 3 else "warning",
                "metric": _("%(now)s now, +%(rate)s/day → ~%(proj)s in 90 days") % {
                    "now": _human(t.get("bytes", 0)),
                    "rate": _human(per_day),
                    "proj": _human(projected)},
                "threshold": _("growing faster than %s/day") % _human(min_mb_per_day * 1024 * 1024),
                "recommendation": _(
                    "Measured over %(d).1f days of snapshots. Decide now whether this table needs a "
                    "retention policy, archival, or partitioning — it is much cheaper to do before "
                    "it is large.") % {"d": days},
                "fix_kind": "none",
            })
        # Rank by growth rate, not by name: the cap of 10 has to keep the ten
        # fastest-growing tables, not the ten alphabetically first.
        out.sort(key=lambda r: r["_per_day"], reverse=True)
        return [{k: v for k, v in r.items() if k != "_per_day"} for r in out[:10]]

    def _detect_h2_vacuum_trend(self):
        oldest, newest = self._snapshot_pair()
        if not (oldest and newest):
            return []
        old_dead = oldest.dead_tuples_total or 0
        new_dead = newest.dead_tuples_total or 0
        live = newest.live_tuples_total or 1
        growth_factor = self._threshold("h2_factor", 1.5)
        if new_dead < 50000 or old_dead <= 0 or new_dead < old_dead * growth_factor:
            return []
        return [{
            "target_kind": "other",
            "target_ref": "autovacuum",
            "metric": _("dead rows %(a)s → %(b)s (%(pct)s%% of live)") % {
                "a": "{:,}".format(int(old_dead)), "b": "{:,}".format(int(new_dead)),
                "pct": round(new_dead / live * 100, 1)},
            "threshold": _("dead-row total growing more than %.1fx between snapshots") % growth_factor,
            "recommendation": _(
                "Dead rows are accumulating faster than autovacuum removes them, so tables are "
                "bloating. Lower autovacuum_vacuum_scale_factor on the busiest tables, raise "
                "autovacuum_max_workers, or increase maintenance_work_mem so each pass finishes."),
            "fix_kind": "config",
        }]


def _human(n):
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return "%.0f %s" % (n, unit)
        n /= 1024
    return "%.1f PB" % n
