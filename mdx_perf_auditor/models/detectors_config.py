"""Group F — Configuration & environment. Advisory only. Reads pg_settings, the
server config, and /proc/meminfo when available. Never changes anything."""

from odoo import _, models
from odoo.tools import SQL, config


def _read_mem_total_bytes():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _pg_setting_bytes(setting, unit):
    try:
        val = float(setting)
    except (TypeError, ValueError):
        return None
    mult = {"8kB": 8192, "kB": 1024, "16kB": 16384, "MB": 1024 ** 2,
            "GB": 1024 ** 3, "": 1, "B": 1}.get((unit or "").strip())
    return val * mult if mult else None


class PerfAuditRunConfig(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "F1", "group": "config", "method": "_detect_f1_pg_memory",
             "title": _("PostgreSQL memory setting looks low"), "severity": "warning"},
            {"code": "F2", "group": "config", "method": "_detect_f2_pg_planner",
             "title": _("PostgreSQL planner setting"), "severity": "info"},
            {"code": "F3", "group": "config", "method": "_detect_f3_pgss",
             "title": _("pg_stat_statements not installed"), "severity": "info"},
            {"code": "F4", "group": "config", "method": "_detect_f4_prod_flags",
             "title": _("Development setting active"), "severity": "warning"},
            {"code": "F5", "group": "config", "method": "_detect_f5_profiling_left_on",
             "title": _("Profiling left enabled"), "severity": "warning"},
            {"code": "F6", "group": "config", "method": "_detect_f6_workers",
             "title": _("Server worker configuration"), "severity": "warning"},
        ]

    def _pg_settings(self, names):
        rows = self._pg(SQL(
            "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s)", list(names)
        ))
        return {r["name"]: r for r in rows}

    # ------------------------------------------------------------------
    def _detect_f1_pg_memory(self):
        s = self._pg_settings(["shared_buffers", "effective_cache_size",
                               "work_mem", "maintenance_work_mem"])
        ram = _read_mem_total_bytes()
        out = []

        sb = _pg_setting_bytes(s.get("shared_buffers", {}).get("setting"),
                               s.get("shared_buffers", {}).get("unit"))
        if sb is not None and ram and sb < ram * 0.15:
            out.append({
                "target_kind": "config", "target_ref": "postgresql.shared_buffers",
                "metric": _("%.0f MB of %.0f GB RAM (%.0f%%)") % (
                    sb / 1024 / 1024, ram / 1024 ** 3, sb / ram * 100),
                "threshold": _("recommended ~25%% of server RAM"),
                "recommendation": _("Raise shared_buffers to about %.0f MB and restart PostgreSQL.")
                % (ram * 0.25 / 1024 / 1024),
                "fix_kind": "config",
            })
        elif sb is not None and not ram and sb < 200 * 1024 * 1024:
            out.append({
                "target_kind": "config", "target_ref": "postgresql.shared_buffers",
                "metric": _("%.0f MB") % (sb / 1024 / 1024),
                "threshold": _("recommended ~25%% of server RAM"),
                "recommendation": _("shared_buffers is at the default. On a dedicated DB server set "
                                    "it to about a quarter of RAM."),
                "fix_kind": "config",
            })

        wm = _pg_setting_bytes(s.get("work_mem", {}).get("setting"),
                               s.get("work_mem", {}).get("unit"))
        if wm is not None and wm < 8 * 1024 * 1024:
            out.append({
                "target_kind": "config", "target_ref": "postgresql.work_mem",
                "metric": _("%.0f MB") % (wm / 1024 / 1024),
                "threshold": _("Odoo reporting queries want 16–64 MB"),
                "recommendation": _("Sorts and hash joins in reports spill to disk below this. Set "
                                    "work_mem to 32 MB (per-operation — keep max_connections in mind)."),
                "fix_kind": "config",
            })

        mwm = _pg_setting_bytes(s.get("maintenance_work_mem", {}).get("setting"),
                                s.get("maintenance_work_mem", {}).get("unit"))
        if mwm is not None and mwm < 256 * 1024 * 1024:
            out.append({
                "target_kind": "config", "target_ref": "postgresql.maintenance_work_mem",
                "metric": _("%.0f MB") % (mwm / 1024 / 1024),
                "threshold": _("512 MB – 2 GB speeds up VACUUM and CREATE INDEX"),
                "recommendation": _("Raise maintenance_work_mem to 512 MB so autovacuum and index "
                                    "builds finish faster."),
                "fix_kind": "config",
            })
        return out

    def _detect_f2_pg_planner(self):
        s = self._pg_settings(["random_page_cost", "max_connections", "effective_cache_size"])
        out = []
        try:
            rpc = float(s.get("random_page_cost", {}).get("setting") or 4)
        except (TypeError, ValueError):
            rpc = 4.0
        if rpc >= 3.0:
            out.append({
                "target_kind": "config", "target_ref": "postgresql.random_page_cost",
                "severity": "info",
                "metric": _("random_page_cost = %s") % rpc,
                "threshold": _("1.1 on SSD / cloud storage"),
                "recommendation": _("The default assumes spinning disks. On SSD or cloud volumes set "
                                    "random_page_cost to 1.1 so the planner picks index scans."),
                "fix_kind": "config",
            })
        try:
            mc = int(s.get("max_connections", {}).get("setting") or 100)
        except (TypeError, ValueError):
            mc = 100
        if mc > 200:
            out.append({
                "target_kind": "config", "target_ref": "postgresql.max_connections",
                "severity": "info",
                "metric": _("max_connections = %d") % mc,
                "threshold": _("100–200 with a pooler in front"),
                "recommendation": _("A high connection cap multiplies work_mem risk. Put PgBouncer "
                                    "in front and bring this down."),
                "fix_kind": "config",
            })
        return out

    def _detect_f3_pgss(self):
        if self._pg(SQL("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")):
            return []
        return [{
            "target_kind": "config", "target_ref": "pg_stat_statements",
            "severity": "info",
            "metric": _("extension not installed"),
            "threshold": _("recommended for query-level insight"),
            "recommendation": _(
                "pg_stat_statements records execution count and timing for every query pattern and "
                "powers detector C6. Add it to shared_preload_libraries, restart, then "
                "CREATE EXTENSION pg_stat_statements."),
            "fix_kind": "config",
        }]

    def _detect_f4_prod_flags(self):
        out = []
        dev = config.get("dev_mode") or []
        if dev:
            out.append({
                "target_kind": "config", "target_ref": "odoo.dev_mode",
                "metric": _("--dev = %s") % ",".join(dev),
                "threshold": _("must be empty in production"),
                "recommendation": _("Developer mode disables asset caching and adds overhead on "
                                    "every request. Remove --dev from the production start command."),
                "fix_kind": "config",
            })
        if config.get("list_db") is not False and not config.get("dbfilter"):
            out.append({
                "target_kind": "config", "target_ref": "odoo.list_db",
                "severity": "info",
                "metric": _("list_db is on with no dbfilter"),
                "threshold": _("set dbfilter or list_db = False in production"),
                "recommendation": _("Not a speed issue directly, but the database selector runs an "
                                    "extra catalog query and exposes database names."),
                "fix_kind": "config",
            })
        return out

    def _detect_f5_profiling_left_on(self):
        until = self.env["ir.config_parameter"].sudo().get_param("base.profiling_enabled_until")
        if not until:
            return []
        try:
            when = self.env["ir.profile"]._enabled_until()
        except Exception:
            when = until
        if not when:
            return []
        return [{
            "target_kind": "config", "target_ref": "base.profiling_enabled_until",
            "metric": _("profiling enabled until %s") % until,
            "threshold": _("enable only for short, targeted windows"),
            "recommendation": _(
                "While this is set, eligible requests are traced and written to ir.profile — real "
                "overhead and a growing table. Clear it once you have the data you need."),
            "fix_kind": "config",
        }]

    def _detect_f6_workers(self):
        out = []
        workers = config.get("workers") or 0
        if not workers:
            out.append({
                "target_kind": "config", "target_ref": "odoo.workers",
                "metric": _("workers = 0 (threaded mode)"),
                "threshold": _("2·CPU + 1 for a production server"),
                "recommendation": _(
                    "Threaded mode serves requests from one process and does not use multiple CPUs. "
                    "Set workers to 2·cores + 1 and configure the matching limit_* options."),
                "fix_kind": "config",
            })
        for key, floor, hint in [
            ("limit_time_real", 120, _("long requests are killed mid-work")),
            ("limit_time_real_cron", 300, _("long crons — including migrations — are killed")),
        ]:
            val = config.get(key) or 0
            if workers and val and val < floor:
                out.append({
                    "target_kind": "config", "target_ref": "odoo.%s" % key,
                    "severity": "info",
                    "metric": _("%s = %ss") % (key, val),
                    "threshold": _("at least %ss") % floor,
                    "recommendation": _("Raise %s — otherwise %s.") % (key, hint),
                    "fix_kind": "config",
                })
        return out
