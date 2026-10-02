"""Group E — Assets & frontend. Size of the compiled backend/front-end asset
bundles and how many modules contribute to them."""

from odoo import _, models
from odoo.tools import SQL


class PerfAuditRunFrontend(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "E1", "group": "frontend", "method": "_detect_e1_bundle_size",
             "title": _("Large compiled asset bundle"), "severity": "warning"},
            {"code": "E2", "group": "frontend", "method": "_detect_e2_bundle_fragmentation",
             "title": _("Many modules contribute to one bundle"), "severity": "info"},
        ]

    def _detect_e1_bundle_size(self):
        mb = self._threshold("e1_mb", 5.0)
        # The LIKE patterns are bound, not inlined: a literal % in the SQL text
        # is a placeholder marker to psycopg2, and Odoo 18 unescapes %% before
        # execute() ever sees it, so the escaped form raises IndexError there.
        rows = self._pg(SQL("""
            SELECT name, file_size
              FROM ir_attachment
             WHERE file_size > %s
               AND (name LIKE %s OR name LIKE %s OR name LIKE %s OR name LIKE %s)
             ORDER BY file_size DESC
             LIMIT 20
        """, int(mb * 1024 * 1024),
             "%assets%.js", "%assets%.css", "%.min.js", "%.min.css"))
        out = []
        for r in rows:
            out.append({
                "target_kind": "bundle",
                "target_ref": (r["name"] or "asset")[:120],
                "metric": _("%.1f MB compiled") % ((r["file_size"] or 0) / 1024 / 1024),
                "threshold": _("bundle over %.0f MB") % mb,
                "recommendation": _(
                    "Every backend page load ships this. Move rarely-used widgets to a lazy bundle, "
                    "and check whether an installed module is pulling a heavy library into the "
                    "global backend assets."),
                "fix_kind": "none",
            })
        return out

    def _detect_e2_bundle_fragmentation(self):
        limit = int(self._threshold("e2_modules", 150))
        rows = self._pg(SQL("""
            SELECT bundle, count(DISTINCT (regexp_match(path, '^([^/]+)/'))[1]) AS modules
              FROM ir_asset
             WHERE active = true
             GROUP BY bundle
            HAVING count(DISTINCT (regexp_match(path, '^([^/]+)/'))[1]) >= %s
             ORDER BY modules DESC
        """, limit))
        return [{
            "target_kind": "bundle",
            "target_ref": r["bundle"],
            "severity": "info",
            "metric": self.env._("%d modules add assets to this bundle") % r["modules"],
            "threshold": self.env._("more than %d contributing modules") % limit,
            "recommendation": self.env._(
                "Not a problem by itself, but a signal of accumulated small customisations. Each one "
                "is parsed and its templates registered on load."),
            "fix_kind": "none",
        } for r in rows]
