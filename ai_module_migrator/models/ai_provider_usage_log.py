from datetime import timedelta

from odoo import api, fields, models


class AiModuleMigratorUsageLog(models.Model):
    _name = "ai.module.migrator.usage.log"
    _description = "AI Addon Migrator Usage Log"
    _order = "id desc"

    provider = fields.Char(string="Provider", required=True, index=True)
    model = fields.Char(string="Model")

    @api.model
    def log_call(self, provider, model=False):
        """Record one AI call for local usage tracking (used where the
        provider does not expose real quota/usage via its API)."""
        self.sudo().create({"provider": provider, "model": model or False})
        # Housekeeping: drop anything older than 8 days so this table never grows unbounded.
        # Two calls to the same provider can legitimately overlap (e.g. a
        # migration job running while someone clicks Settings' "Test
        # Connection"), and a concurrent delete on the same stale rows raises
        # a Postgres serialization failure that poisons the whole calling
        # transaction if left unhandled. This is best-effort cleanup, not
        # correctness-critical, so isolate it in a savepoint: on conflict we
        # just skip cleanup this time and let a later call catch up.
        try:
            with self.env.cr.savepoint():
                stale_before = fields.Datetime.now() - timedelta(days=8)
                stale = self.sudo().search([("provider", "=", provider), ("create_date", "<", stale_before)])
                if stale:
                    stale.unlink()
        except Exception:
            pass

    @api.model
    def usage_stats(self, provider, session_hours=5, weekly_days=7, session_cap=50, weekly_cap=300):
        """Return local session/weekly usage counters for a provider.

        This is a same-instance approximation, not the provider's real quota:
        some providers (e.g. Ollama Cloud) don't expose account-level usage
        through their API, so this counts calls made through this module
        within rolling windows instead.
        """
        now = fields.Datetime.now()
        session_start = now - timedelta(hours=session_hours)
        weekly_start = now - timedelta(days=weekly_days)
        base_domain = [("provider", "=", provider)]

        session_logs = self.sudo().search(base_domain + [("create_date", ">=", session_start)], order="create_date asc")
        weekly_logs = self.sudo().search(base_domain + [("create_date", ">=", weekly_start)], order="create_date asc")

        session_count = len(session_logs)
        weekly_count = len(weekly_logs)
        session_cap = session_cap or 1
        weekly_cap = weekly_cap or 1

        return {
            "session_used": session_count,
            "session_cap": session_cap,
            "session_pct": round(min(100.0, (session_count / session_cap) * 100.0), 1),
            "session_reset": (session_logs[0].create_date + timedelta(hours=session_hours)) if session_logs else False,
            "weekly_used": weekly_count,
            "weekly_cap": weekly_cap,
            "weekly_pct": round(min(100.0, (weekly_count / weekly_cap) * 100.0), 1),
            "weekly_reset": (weekly_logs[0].create_date + timedelta(days=weekly_days)) if weekly_logs else False,
        }
