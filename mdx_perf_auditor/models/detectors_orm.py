"""Group B — ORM & model design. Static analysis of the loaded registry: no SQL
writes. These findings are advice for whoever maintains the CUSTOM code, so every
detector is limited to models that a non-core module contributes to."""

import os

from odoo import _, models
from odoo.tools import SQL
from odoo.modules.module import get_module_path


class PerfAuditRunOrm(models.Model):
    _inherit = "perf.audit.run"

    def _get_detectors(self):
        return super()._get_detectors() + [
            {"code": "B1", "group": "orm", "method": "_detect_b1_computed_order",
             "title": _("Model sorts on a computed field"), "severity": "warning"},
            {"code": "B2", "group": "orm", "method": "_detect_b2_compute_no_depends",
             "title": _("Computed field without @api.depends"), "severity": "info"},
            {"code": "B4", "group": "orm", "method": "_detect_b4_related_in_list",
             "title": _("Non-stored related field in a list view"), "severity": "warning"},
            {"code": "B5", "group": "orm", "method": "_detect_b5_create_not_multi",
             "title": _("create() overridden without model_create_multi"), "severity": "info"},
            {"code": "B6", "group": "orm", "method": "_detect_b6_view_depth",
             "title": _("Deep view-inheritance chain"), "severity": "info"},
            {"code": "B7", "group": "orm", "method": "_detect_b7_log_access",
             "title": _("Large table carries _log_access overhead"), "severity": "info"},
        ]

    def _row_counts(self):
        return {
            r["relname"]: r["n_live_tup"]
            for r in self._pg(SQL("SELECT relname, n_live_tup FROM pg_stat_user_tables"))
        }

    def _core_addons_dirs(self):
        """Directories that hold Odoo core / Enterprise modules, derived from the
        location of modules that only ship with Odoo."""
        dirs = set()
        for probe in ("base", "web", "web_tour", "mail", "account", "sale", "stock"):
            try:
                p = get_module_path(probe, display_warning=False)
            except Exception:
                p = None
            if p:
                dirs.add(os.path.dirname(os.path.normpath(p)))
        return dirs

    def _custom_module_names(self):
        core_dirs = self._core_addons_dirs()
        names = set()
        for m in self.env["ir.module.module"].sudo().search([("state", "=", "installed")]):
            try:
                path = get_module_path(m.name, display_warning=False) or ""
            except Exception:
                path = ""
            if not path:
                continue
            if os.path.dirname(os.path.normpath(path)) not in core_dirs:
                names.add(m.name)
        return names

    def _custom_models(self):
        custom = self._custom_module_names()
        result = []
        if not custom:
            return result
        for name, model in self.env.registry.items():
            if model._abstract or model._transient:
                continue
            mro = model.mro() if hasattr(model, "mro") else type(model).__mro__
            if any(getattr(k, "_module", None) in custom for k in mro):
                result.append((name, model))
        return result

    # ------------------------------------------------------------------
    def _detect_b1_computed_order(self):
        out = []
        custom_mods = self._custom_module_names()
        for name, model in self._custom_models():
            order = self._model_attr(name, model, "_order", "id") or "id"
            for part in [p.split()[0].strip() for p in order.split(",") if p.strip()]:
                f = model._fields.get(part)
                if f and f.compute and not f.store and getattr(f, "_module", None) in custom_mods:
                    out.append({
                        "target_kind": "model",
                        "target_ref": "%s (_order: %s)" % (name, part),
                        "metric": _("_order = %r, %r is computed and not stored") % (order, part),
                        "threshold": _("the sort column of a model should be a stored column"),
                        "recommendation": _(
                            "Every search on %s that uses the default order triggers the compute for "
                            "the whole result set. Store the field (store=True + @api.depends) or "
                            "order by a real column.") % name,
                        "fix_kind": "none",
                    })
        return out

    def _detect_b2_compute_no_depends(self):
        out = []
        custom_mods = self._custom_module_names()
        for name, model in self._custom_models():
            bad = []
            for fname, f in model._fields.items():
                if getattr(f, "_module", None) not in custom_mods:
                    continue  # only judge fields the custom module itself declares
                if not f.compute or f.related or f.related_field:
                    continue
                if not f.store and f.search:
                    # Compute-on-read backed by a search method is a deliberate
                    # pattern: the value is derived per read and the filter is
                    # served by the search method, so there is nothing for
                    # @api.depends to invalidate.
                    continue
                if callable(f.compute):  # inline callable, can't introspect
                    continue
                method = getattr(model, f.compute, None)
                if (f._depends or getattr(method, "_depends", None)
                        or getattr(method, "_depends_context", None)):
                    continue
                bad.append(fname)
            if bad:
                out.append({
                    "target_kind": "model",
                    "target_ref": name,
                    "metric": _("%d field(s): %s") % (len(bad), ", ".join(sorted(bad)[:8])),
                    "threshold": _("computed fields declare their dependencies"),
                    "recommendation": _(
                        "Without @api.depends these fields never recompute automatically, so they "
                        "either show stale values or recompute on every read. Declare the "
                        "dependencies, or document why the field is compute-on-read."),
                    "fix_kind": "none",
                })
        return out

    def _detect_b4_related_in_list(self):
        View = self.env["ir.ui.view"].sudo()
        custom_mods = self._custom_module_names()
        out = []
        for name, model in self._custom_models():
            related_unstored = {
                fn for fn, f in model._fields.items()
                if f.related and not f.store and not getattr(f, "inherited", False)
                and getattr(f, "_module", None) in custom_mods
            }
            if not related_unstored:
                continue
            hit = set()
            for v in View.search([("model", "=", name), ("type", "=", "list")]):
                arch = v.arch_db or ""
                for fn in related_unstored:
                    if ('name="%s"' % fn) in arch:
                        hit.add(fn)
            if hit:
                out.append({
                    "target_kind": "model",
                    "target_ref": name,
                    "metric": _("columns: %s") % ", ".join(sorted(hit)),
                    "threshold": _("list-view columns should be stored"),
                    "recommendation": _(
                        "Each of these columns walks a relation for every row on every page of the "
                        "list. Add store=True to the related field so it is read straight from the "
                        "table."),
                    "fix_kind": "none",
                })
        return out

    def _detect_b5_create_not_multi(self):
        out = []
        custom_mods = self._custom_module_names()
        for name, model in self._custom_models():
            create = getattr(model, "create", None)
            if create is None or getattr(create, "_api_model", False):
                continue
            mod = getattr(create, "__module__", "") or ""
            base = mod.replace("odoo.addons.", "").split(".")[0]
            if base and base not in custom_mods:
                continue
            out.append({
                "target_kind": "model",
                "target_ref": name,
                "metric": _("create() defined in %s") % (base or mod),
                "threshold": _("create() should accept a list of values"),
                "recommendation": _(
                    "This override runs once per record instead of once per batch. Switch the "
                    "signature to @api.model_create_multi(vals_list) — imports, onboarding and "
                    "server actions all create in batches."),
                "fix_kind": "none",
            })
        return out

    def _detect_b6_view_depth(self):
        limit = self._threshold("b6_depth", 12)
        View = self.env["ir.ui.view"].sudo()
        rows = self._pg(SQL("""
            SELECT inherit_id, count(*) AS n
              FROM ir_ui_view
             WHERE inherit_id IS NOT NULL AND active = true
             GROUP BY inherit_id
            HAVING count(*) >= %s
             ORDER BY n DESC
        """, limit))
        out = []
        for r in rows:
            base = View.browse(r["inherit_id"])
            if not base.exists():
                continue
            out.append({
                "target_kind": "other",
                "target_ref": "%s (%s)" % (base.name or base.id, base.model or ""),
                "metric": _("%d active views inherit this one") % r["n"],
                "threshold": _("more than %d inheriting views") % int(limit),
                "recommendation": _(
                    "Every one of these xpath patches is applied each time the view is built. If "
                    "this is a hot view, consolidate overrides or combine the modules that add them."),
                "fix_kind": "none",
            })
        return out

    def _detect_b7_log_access(self):
        min_rows = self._threshold("b7_min_rows", 1000000)
        counts = self._row_counts()
        out = []
        for name, model in self.env.registry.items():
            if model._abstract or model._transient or not model._auto:
                continue
            if not self._model_attr(name, model, "_log_access", True):
                continue
            rows = counts.get(model._table, 0)
            if rows < min_rows:
                continue
            out.append({
                "target_kind": "model",
                "target_ref": name,
                "metric": _("%s rows, with create_uid/write_uid/create_date/write_date")
                % "{:,}".format(int(rows)),
                "threshold": _("_log_access on a table over %s rows") % "{:,}".format(int(min_rows)),
                "recommendation": _(
                    "The four audit columns and their write cost add up on a table this size. If row "
                    "history is not needed here, set _log_access = False on the model."),
                "fix_kind": "none",
            })
        return out
