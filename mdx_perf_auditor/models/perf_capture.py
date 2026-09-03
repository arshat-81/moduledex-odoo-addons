"""Deep-analysis capture window.

Deep detectors need real query plans, and a plan needs a real statement with real
parameters. Odoo's own profiler already records exactly that (``ir.profile.sql``
keeps ``full_query``), so instead of writing another instrumentation layer this
module simply turns Odoo's profiler on for a bounded window and analyses what it
collects.

The hook below is the smallest possible seam: it fills in the session keys that
``Request._get_profiler_context_manager`` already looks for, and does nothing at
all outside a capture window. State is cached per worker process so a request
never pays for a database read.
"""

import logging
import random
import time
from datetime import datetime, timedelta

import odoo
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.http import Request
from odoo.tools import profiler as profiler_tools

_logger = logging.getLogger(__name__)

P_UNTIL = "mdx_perf_auditor.capture_until"
P_RATE = "mdx_perf_auditor.capture_rate"
P_STARTED = "mdx_perf_auditor.capture_started"

_STATE = {}  # dbname -> {"until": str, "rate": float, "checked": float}
_REFRESH_SECONDS = 20
MAX_MINUTES = 120


def _capture_state(dbname):
    """Cheap, per-process, refreshed at most every _REFRESH_SECONDS."""
    now = time.time()
    st = _STATE.get(dbname)
    if st and (now - st["checked"]) < _REFRESH_SECONDS:
        return st
    until, rate = "", 0.0
    try:
        with odoo.sql_db.db_connect(dbname).cursor() as cr:
            cr.execute(
                "SELECT key, value FROM ir_config_parameter WHERE key IN %s",
                ((P_UNTIL, P_RATE),),
            )
            vals = dict(cr.fetchall())
        until = vals.get(P_UNTIL) or ""
        rate = float(vals.get(P_RATE) or 0.0)
    except Exception:
        pass  # database busy / not ready — treat as "no capture"
    st = {"until": until, "rate": rate, "checked": now}
    _STATE[dbname] = st
    return st


def _invalidate(dbname):
    _STATE.pop(dbname, None)


# The capture hook rides on a private core method. If a future Odoo release
# moves or renames it, the module must keep working with capture unavailable
# rather than failing to load.
_SEAM = "_get_profiler_context_manager"
_original_profiler_cm = getattr(Request, _SEAM, None)
CAPTURE_SUPPORTED = callable(_original_profiler_cm)

if not CAPTURE_SUPPORTED:
    _logger.warning(
        "mdx_perf_auditor: Request.%s is missing on this Odoo build, so traffic "
        "capture is disabled. Every other check still runs; deep query analysis "
        "will report that it has nothing to analyse.", _SEAM)


def _mdx_profiler_cm(self):
    try:
        if self.db and not self.session.get("profile_session"):
            st = _capture_state(self.db)
            if st["rate"] > 0 and st["until"] and str(datetime.now()) < st["until"]:
                if random.random() < st["rate"]:
                    self.session["profile_session"] = profiler_tools.make_session("mdx-deep")
                    self.session["profile_expiration"] = st["until"]
                    self.session["profile_collectors"] = ["sql", "traces_async"]
                    self.session["profile_params"] = {}
    except Exception:
        _logger.debug("mdx_perf_auditor: capture hook skipped", exc_info=True)
    return _original_profiler_cm(self)


if CAPTURE_SUPPORTED and not getattr(_original_profiler_cm, "_mdx_patched", False):
    _mdx_profiler_cm._mdx_patched = True
    setattr(Request, _SEAM, _mdx_profiler_cm)


class PerfCapture(models.AbstractModel):
    _name = "perf.capture"
    _description = "Deep Analysis Capture Window"

    # ------------------------------------------------------------------
    @api.model
    def _param(self):
        return self.env["ir.config_parameter"].sudo()

    @api.model
    def start(self, minutes=10, rate=1.0):
        if not self.env.user._is_system():
            raise UserError(_("Only a Settings administrator can start a capture."))
        if not CAPTURE_SUPPORTED:
            raise UserError(_(
                "Traffic capture is not available on this Odoo build — the profiler "
                "hook this module relies on is missing. Enable profiling manually "
                "under Settings > Technical instead."))
        minutes = max(1, min(int(minutes or 10), MAX_MINUTES))
        rate = max(0.01, min(float(rate or 1.0), 1.0))
        now = fields.Datetime.now()
        until = now + timedelta(minutes=minutes)
        p = self._param()
        p.set_param(P_UNTIL, fields.Datetime.to_string(until))
        p.set_param(P_RATE, str(rate))
        p.set_param(P_STARTED, fields.Datetime.to_string(now))
        _invalidate(self.env.cr.dbname)
        _logger.info("mdx_perf_auditor: capture started for %s minutes at rate %.2f", minutes, rate)
        return self.status()

    @api.model
    def stop(self):
        if not self.env.user._is_system():
            raise UserError(_("Only a Settings administrator can stop a capture."))
        p = self._param()
        p.set_param(P_UNTIL, "")
        p.set_param(P_RATE, "0")
        _invalidate(self.env.cr.dbname)
        return self.status()

    @api.model
    def status(self):
        p = self._param()
        until = p.get_param(P_UNTIL) or ""
        started = p.get_param(P_STARTED) or ""
        active = bool(until) and fields.Datetime.to_string(fields.Datetime.now()) < until
        # Only profiles from our own capture window count. Anything older was
        # recorded by somebody profiling by hand and is not ours to analyse.
        profiles = self.env["ir.profile"].sudo().search_count(
            [("create_date", ">=", started)]
        ) if started else 0
        return {
            "active": active,
            "until": until,
            "started": started,
            "rate": float(p.get_param(P_RATE) or 0.0),
            "profiles": profiles,
            "supported": CAPTURE_SUPPORTED,
            "seconds_left": max(
                0,
                int((fields.Datetime.to_datetime(until) - fields.Datetime.now()).total_seconds())
            ) if active else 0,
        }

    @api.model
    def captured_profiles(self, limit=150):
        """Profiles recorded since the last capture started. Empty if none was.

        ``ir.profile.sql`` is declared ``prefetch=False``, so each record read is
        its own SELECT over a large text column — the limit keeps a deep pass from
        costing more than the problem it is looking for."""
        started = self._param().get_param(P_STARTED)
        if not started:
            return self.env["ir.profile"].sudo().browse()
        return self.env["ir.profile"].sudo().search(
            [("create_date", ">=", started)], limit=limit, order="duration desc")

    @api.model
    def cron_expire(self):
        """Housekeeping: clear the rate once the window has passed."""
        p = self._param()
        until = p.get_param(P_UNTIL)
        if until and fields.Datetime.to_string(fields.Datetime.now()) >= until:
            p.set_param(P_RATE, "0")
            p.set_param(P_UNTIL, "")
            _invalidate(self.env.cr.dbname)
