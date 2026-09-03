/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { _t } from "@web/core/l10n/translation";

const LS_KEY = "mdx_perf_dashboard_prefs";
const SEV_RANK = { critical: 0, warning: 1, info: 2 };
const SEV_LETTER = { critical: "C", warning: "W", info: "A" };

export class PerfDashboard extends Component {
    static template = "mdx_perf_auditor.PerfDashboard";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");

        const prefs = this._loadPrefs();
        this.state = useState({
            loading: true,
            running: false,
            data: null,
            severity: prefs.severity || "all",
            category: prefs.category || null,
            groupBy: prefs.groupBy || "category",
            search: "",
            newOnly: prefs.newOnly || false,
            expanded: {},
            collapsed: prefs.collapsed || {},
            captureMinutes: prefs.captureMinutes || 10,
        });

        onWillStart(() => this.load());
    }

    // ---- data ------------------------------------------------------------
    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call("perf.audit.run", "get_dashboard_data", []);
        this.state.loading = false;
    }

    async runAudit(depth = "quick") {
        if (this.state.running) return;
        this.state.running = depth;
        try {
            await this.orm.call("perf.audit.run", "action_run_now", ["manual", depth]);
            await this.load();
            this.notification.add(
                depth === "deep" ? _t("Deep analysis complete.") : _t("Audit complete."),
                { type: "success" }
            );
        } catch (e) {
            this.notification.add(_t("The audit did not finish. Check the server log."), {
                type: "danger",
            });
            throw e;
        } finally {
            this.state.running = false;
        }
    }

    // ---- capture window --------------------------------------------
    get capture() {
        return (this.state.data && this.state.data.capture) || { active: false, profiles: 0 };
    }

    async startCapture() {
        try {
            await this.orm.call("perf.capture", "start", [this.state.captureMinutes, 1.0]);
            await this.load();
            this.notification.add(
                _t("Capturing. Use the slow parts of Odoo, then run the deep analysis."),
                { type: "success" }
            );
        } catch (e) {
            this.notification.add(_t("Could not start the capture."), { type: "danger" });
            throw e;
        }
    }

    async stopCapture() {
        await this.orm.call("perf.capture", "stop", []);
        await this.load();
    }

    setCaptureMinutes(ev) {
        this.state.captureMinutes = Math.max(1, Math.min(120, parseInt(ev.target.value, 10) || 10));
    }

    get captureLeft() {
        const s = this.capture.seconds_left || 0;
        if (!s) return "";
        const m = Math.floor(s / 60);
        return m >= 1 ? `${m}m left` : `${s}s left`;
    }

    async findingAction(id, method) {
        try {
            const res = await this.orm.call("perf.audit.run", "dashboard_finding_action", [id, method]);
            if (res && typeof res === "object" && res.type) {
                await this.action.doAction(res);
            }
        } catch (e) {
            this.notification.add(e.data && e.data.message ? e.data.message : _t("Action failed."), {
                type: "danger",
            });
        }
        await this.load();
    }

    openList(domain) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Findings"),
            res_model: "perf.audit.finding",
            views: [[false, "list"], [false, "form"]],
            domain: domain || [["status", "in", ["open", "acknowledged"]]],
            target: "current",
        });
    }

    openHistory() {
        this.action.doAction("mdx_perf_auditor.action_perf_audit_run");
    }

    openFinding(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "perf.audit.finding",
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    // ---- prefs ----------------------------------------------------------
    _loadPrefs() {
        try {
            return JSON.parse(window.localStorage.getItem(LS_KEY)) || {};
        } catch {
            return {};
        }
    }
    _savePrefs() {
        try {
            window.localStorage.setItem(
                LS_KEY,
                JSON.stringify({
                    severity: this.state.severity,
                    category: this.state.category,
                    groupBy: this.state.groupBy,
                    newOnly: this.state.newOnly,
                    collapsed: this.state.collapsed,
                    captureMinutes: this.state.captureMinutes,
                })
            );
        } catch {
            /* private mode — ignore */
        }
    }

    // ---- filter / group controls --------------------------------------
    setSeverity(sev) {
        this.state.severity = this.state.severity === sev ? "all" : sev;
        this._savePrefs();
    }
    toggleCategory(code) {
        this.state.category = this.state.category === code ? null : code;
        this._savePrefs();
    }
    setGroupBy(g) {
        this.state.groupBy = g;
        this._savePrefs();
    }
    toggleNewOnly() {
        this.state.newOnly = !this.state.newOnly;
        this._savePrefs();
    }
    onSearch(ev) {
        this.state.search = ev.target.value;
    }
    toggleGroup(key) {
        this.state.collapsed[key] = !this.state.collapsed[key];
        this._savePrefs();
    }
    toggleExpand(id) {
        this.state.expanded[id] = !this.state.expanded[id];
    }
    clearFilters() {
        this.state.severity = "all";
        this.state.category = null;
        this.state.newOnly = false;
        this.state.search = "";
        this._savePrefs();
    }

    // ---- derived view state -----------------------------------------
    get run() {
        return this.state.data && this.state.data.run;
    }
    get hasRun() {
        return this.state.data && this.state.data.has_run;
    }
    get categories() {
        const d = this.state.data;
        if (!d) return [];
        return Object.entries(d.categories).map(([code, label]) => {
            const c = d.run.counts[code] || {};
            return {
                code,
                label,
                score: d.run.scores[code],
                critical: c.critical || 0,
                warning: c.warning || 0,
                info: c.info || 0,
                total: (c.critical || 0) + (c.warning || 0) + (c.info || 0),
            };
        });
    }
    get filterActive() {
        return (
            this.state.severity !== "all" ||
            this.state.category ||
            this.state.newOnly ||
            this.state.search
        );
    }
    get filteredFindings() {
        const d = this.state.data;
        if (!d) return [];
        const q = this.state.search.trim().toLowerCase();
        return d.findings.filter((f) => {
            if (this.state.severity !== "all" && f.severity !== this.state.severity) return false;
            if (this.state.category && f.category !== this.state.category) return false;
            if (this.state.newOnly && !f.is_new) return false;
            if (q) {
                const hay = (f.name + " " + f.target_ref + " " + f.metric + " " + f.detector_code).toLowerCase();
                if (!hay.includes(q)) return false;
            }
            return true;
        });
    }
    get groups() {
        const d = this.state.data;
        if (!d) return [];
        const by = this.state.groupBy;
        const labelOf = (f) => {
            if (by === "category") return d.categories[f.category] || f.category;
            if (by === "severity") return d.severity_labels[f.severity] || f.severity;
            return f.target_kind;
        };
        const keyOf = (f) => (by === "category" ? f.category : by === "severity" ? f.severity : f.target_kind);
        const map = new Map();
        for (const f of this.filteredFindings) {
            const k = keyOf(f);
            if (!map.has(k)) map.set(k, { key: k, label: labelOf(f), items: [] });
            map.get(k).items.push(f);
        }
        const groups = [...map.values()];
        groups.sort((a, b) => {
            if (by === "severity") return (SEV_RANK[a.key] ?? 9) - (SEV_RANK[b.key] ?? 9);
            return b.items.length - a.items.length;
        });
        return groups;
    }
    get resolvedRecent() {
        return (this.state.data && this.state.data.resolved_recent) || [];
    }
    get newCount() {
        const d = this.state.data;
        return d ? d.findings.filter((f) => f.is_new).length : 0;
    }
    _sevTotal(sev) {
        return this.categories.reduce((a, c) => a + (c[sev] || 0), 0);
    }
    get critCount() {
        return this._sevTotal("critical");
    }
    get warnCount() {
        return this._sevTotal("warning");
    }
    get infoCount() {
        return this._sevTotal("info");
    }

    // ---- trend sparkline --------------------------------------------
    get trend() {
        return (this.state.data && this.state.data.trend) || [];
    }
    get trendLine() {
        const t = this.trend;
        if (t.length < 2) return "";
        const w = 100, h = 30;
        const step = w / (t.length - 1);
        return t.map((p, i) => `${(i * step).toFixed(2)},${(h - (p.score / 100) * h).toFixed(2)}`).join(" ");
    }
    get trendArea() {
        const line = this.trendLine;
        if (!line) return "";
        const pts = line.split(" ");
        return `0,30 ${line} ${pts[pts.length - 1].split(",")[0]},30`;
    }

    // ---- largest tables bar chart ---------------------------------
    get tables() {
        const raw = (this.state.data && this.state.data.biggest_tables) || [];
        const top = raw.slice(0, 8);
        const max = Math.max(1, ...top.map((t) => t.bytes || 0));
        return top.map((t) => ({ ...t, pct: Math.round(((t.bytes || 0) / max) * 100) }));
    }

    // ---- formatting helpers --------------------------------------
    sevLetter(sev) {
        return SEV_LETTER[sev] || "?";
    }
    sevLabel(sev) {
        return (this.state.data && this.state.data.severity_labels[sev]) || sev;
    }
    scoreColor(n) {
        if (n >= 85) return "var(--perf-ok)";
        if (n >= 60) return "var(--perf-warn)";
        return "var(--perf-crit)";
    }
    scoreClass(n) {
        return n >= 85 ? "is-ok" : n >= 60 ? "is-warn" : "is-crit";
    }
    get deltaText() {
        const d = this.run && this.run.delta;
        if (d === null || d === undefined) return "";
        if (d === 0) return _t("no change since last audit");
        return (d > 0 ? "▲ " : "▼ ") + Math.abs(d) + _t(" since last audit");
    }
    get deltaClass() {
        const d = this.run && this.run.delta;
        if (!d) return "text-muted";
        return d > 0 ? "perf-up" : "perf-down";
    }
    fixVerb(kind) {
        return {
            create_index: _t("Create index"),
            analyze: _t("Run ANALYZE"),
            vacuum: _t("Run VACUUM"),
        }[kind] || _t("Apply fix");
    }
}

registry.category("actions").add("mdx_perf_auditor.dashboard", PerfDashboard);
