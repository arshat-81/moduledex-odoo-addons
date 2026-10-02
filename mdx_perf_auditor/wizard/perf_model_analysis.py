"""On-demand deep dive into a single model: its storage, its indexes, its
compute graph, and any slow captured statement that touches its table."""

import json
from collections import defaultdict

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.tools import SQL


def _h(n):
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return "%.0f %s" % (n, unit)
        n /= 1024
    return "%.1f PB" % n


class PerfModelAnalysis(models.TransientModel):
    _name = "perf.model.analysis"
    _description = "Model Deep Dive"

    model_id = fields.Many2one("ir.model", required=True, ondelete="cascade")
    report_html = fields.Html(compute="_compute_report", sanitize=False, readonly=True)

    @api.depends("model_id")
    def _compute_report(self):
        for rec in self:
            rec.report_html = rec._build() if rec.model_id else False

    # ------------------------------------------------------------------
    def _pg(self, query):
        self.env.cr.execute(query)
        return self.env.cr.dictfetchall()

    def _build(self):
        self.ensure_one()
        name = self.model_id.model
        if name not in self.env:
            return Markup("<p>%s</p>") % _("That model is not in the registry.")
        model = self.env[name]
        table = model._table
        parts = [Markup('<div class="o_perf_dive">')]

        # ---- storage ---------------------------------------------------
        stats = self._pg(SQL("""
            SELECT n_live_tup, n_dead_tup, seq_scan, COALESCE(idx_scan,0) AS idx_scan,
                   last_autovacuum, last_autoanalyze,
                   pg_total_relation_size(relid) AS total_bytes,
                   pg_relation_size(relid) AS heap_bytes
              FROM pg_stat_user_tables WHERE relname = %s
        """, table))
        s = stats[0] if stats else {}
        parts.append(self._section(_("Storage"), [
            (_("Table"), table),
            (_("Rows (live / dead)"), "%s / %s" % (
                "{:,}".format(int(s.get("n_live_tup") or 0)),
                "{:,}".format(int(s.get("n_dead_tup") or 0)))),
            (_("Size (total / heap)"), "%s / %s" % (
                _h(s.get("total_bytes")), _h(s.get("heap_bytes")))),
            (_("Scans (seq / index)"), "%s / %s" % (
                "{:,}".format(int(s.get("seq_scan") or 0)),
                "{:,}".format(int(s.get("idx_scan") or 0)))),
            (_("Last autovacuum"), str(s.get("last_autovacuum") or _("never"))),
            (_("Last autoanalyze"), str(s.get("last_autoanalyze") or _("never"))),
            # read from the recordset: _order may be a property (see account)
            (_("_order"), model._order if isinstance(model._order, str) else "id"),
            (_("_log_access"), _("yes") if model._log_access else _("no")),
        ]))

        # ---- field inventory -------------------------------------------
        f_all = model._fields
        stored = [f for f in f_all.values() if f.store]
        computed = [f for f in f_all.values() if f.compute]
        comp_stored = [f for f in computed if f.store]
        related = [f for f in f_all.values() if f.related]
        rel_unstored = [f for f in related if not f.store]
        indexed = [f for f in stored if f.index]
        parts.append(self._section(_("Fields"), [
            (_("Total"), len(f_all)),
            (_("Stored columns"), len(stored)),
            (_("Indexed"), len(indexed)),
            (_("Computed (stored / on-the-fly)"), "%d / %d" % (len(comp_stored), len(computed) - len(comp_stored))),
            (_("Related (stored / on-the-fly)"), "%d / %d" % (
                len(related) - len(rel_unstored), len(rel_unstored))),
        ]))
        if rel_unstored:
            parts.append(self._note(_(
                "Non-stored related fields walk a relation on every read: %s")
                % ", ".join(sorted(f.name for f in rel_unstored)[:12])))

        # ---- indexes ----------------------------------------------------
        idx = self._pg(SQL("""
            SELECT s.indexrelname AS name, s.idx_scan,
                   pg_relation_size(s.indexrelid) AS bytes,
                   pg_get_indexdef(s.indexrelid) AS def
              FROM pg_stat_user_indexes s
             WHERE s.relname = %s
             ORDER BY pg_relation_size(s.indexrelid) DESC
        """, table))
        if idx:
            rows = [(
                Markup("<code>%s</code>") % escape(i["name"]),
                "%s · %s scans" % (_h(i["bytes"]), "{:,}".format(int(i["idx_scan"] or 0))),
            ) for i in idx]
            parts.append(self._section(_("Indexes (%d)") % len(idx), rows))
            unused = [i["name"] for i in idx if not i["idx_scan"]]
            if unused:
                parts.append(self._note(_("Never scanned: %s") % ", ".join(unused[:8])))

        # ---- unindexed foreign keys -------------------------------------
        fks = self._pg(SQL("""
            SELECT a.attname AS col
              FROM pg_constraint c
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
             WHERE c.contype = 'f' AND c.conrelid = %s::regclass
               AND NOT EXISTS (SELECT 1 FROM pg_index i
                                WHERE i.indrelid = c.conrelid AND i.indkey[0] = a.attnum)
        """, table))
        if fks:
            parts.append(self._note(_("Foreign keys with no index: %s")
                                    % ", ".join(f["col"] for f in fks)))

        # ---- compute graph ----------------------------------------------
        triggers = defaultdict(set)
        for fname, f in f_all.items():
            if not f.compute:
                continue
            deps = f._depends
            if not deps and isinstance(f.compute, str):
                deps = getattr(getattr(model, f.compute, None), "_depends", None)
            # @api.depends also accepts a single callable returning the deps
            if not deps or callable(deps):
                continue
            for dep in deps:
                if callable(dep):
                    continue
                triggers[str(dep).split(".")[0]].add(fname)
        if triggers:
            rows = [
                (Markup("<code>%s</code>") % escape(src),
                 self.env._("recomputes %s") % ", ".join(sorted(dst)[:6]))
                for src, dst in sorted(triggers.items(), key=lambda kv: -len(kv[1]))[:12]
            ]
            parts.append(self._section(_("Write triggers recompute"), rows))

        # ---- captured statements touching this table --------------------
        hits = self._captured_statements(table)
        if hits:
            rows = [(
                "%s ms" % round(h["ms"], 1),
                Markup("<code>%s</code>") % escape(h["sql"][:220]),
            ) for h in hits]
            parts.append(self._section(_("Slowest captured statements on this table"), rows))

        # ---- views -------------------------------------------------------
        views = self.env["ir.ui.view"].sudo().search_count([("model", "=", name)])
        parts.append(self._section(_("Views"), [(_("Definitions on this model"), views)]))

        parts.append(Markup("</div>"))
        return Markup("").join(parts)

    def _captured_statements(self, table):
        out = {}
        for p in self.env["perf.capture"].captured_profiles(limit=80):
            if not p.sql:
                continue
            try:
                entries = json.loads(p.sql)
            except (ValueError, TypeError):
                continue
            for e in entries:
                q = e.get("full_query") or e.get("query") or ""
                if ('"%s"' % table) not in q and (" %s " % table) not in q:
                    continue
                ms = (e.get("time") or 0) * 1000
                key = (e.get("query") or q)[:200]
                if ms > 20 and (key not in out or ms > out[key]["ms"]):
                    out[key] = {"ms": ms, "sql": q}
        return sorted(out.values(), key=lambda r: r["ms"], reverse=True)[:6]

    # ---- html helpers ---------------------------------------------------
    def _section(self, title, rows):
        body = Markup("").join(
            Markup("<tr><td class='o_perf_dive_k'>%s</td><td>%s</td></tr>") % (
                r[0] if isinstance(r[0], Markup) else escape(str(r[0])),
                r[1] if isinstance(r[1], Markup) else escape(str(r[1])),
            )
            for r in rows
        )
        return Markup("<h4>%s</h4><table class='table table-sm o_perf_dive_t'>%s</table>") % (
            escape(title), body)

    def _note(self, text):
        return Markup("<p class='o_perf_dive_note'>%s</p>") % escape(text)

    def action_refresh(self):
        self.ensure_one()
        self._compute_report()
        return {
            "type": "ir.actions.act_window",
            "res_model": "perf.model.analysis",
            "res_id": self.id,
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "new",
        }
