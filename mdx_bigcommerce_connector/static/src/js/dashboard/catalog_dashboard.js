import { Component, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";
import { Layout } from "@web/search/layout";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { _t } from "@web/core/l10n/translation";
import { BigcommerceKpiCard } from "./kpi_card";

export class BigcommerceCatalogDashboard extends Component {
    static template = "mdx_bigcommerce_connector.CatalogDashboard";
    static components = { Layout, BigcommerceKpiCard };
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.display = { controlPanel: {} };

        this.state = useState({
            configId: this.props.action.context?.default_config_id || false,
            configs: [],
            data: null,
        });

        this.syncChartRef = useRef("syncChart");
        this.stockChartRef = useRef("stockChart");
        this.brandChartRef = useRef("brandChart");
        this.categoryChartRef = useRef("categoryChart");
        this.charts = {};

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            this.state.configs = await this.orm.searchRead("bigcommerce.config", [], ["id", "name"]);
            await this.loadData();
        });

        useEffect(() => this.renderCharts());
        onWillUnmount(() => {
            for (const chart of Object.values(this.charts)) {
                chart?.destroy();
            }
        });
    }

    async loadData() {
        this.state.data = await this.orm.call(
            "bigcommerce.product.dashboard", "get_dashboard_data", [],
            { config_id: this.state.configId || undefined },
        );
    }

    async onConfigChange(ev) {
        this.state.configId = ev.target.value ? parseInt(ev.target.value, 10) : false;
        await this.loadData();
    }

    _donut(ref, key, labels, values, colors) {
        this.charts[key]?.destroy();
        if (!ref.el) {
            return;
        }
        this.charts[key] = new Chart(ref.el, {
            type: "doughnut",
            data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, cutout: "68%", plugins: { legend: { position: "bottom" } } },
        });
    }

    _bar(ref, key, labels, values, color) {
        this.charts[key]?.destroy();
        if (!ref.el) {
            return;
        }
        this.charts[key] = new Chart(ref.el, {
            type: "bar",
            data: { labels, datasets: [{ label: _t("Products"), data: values, backgroundColor: color, borderRadius: 6, maxBarThickness: 40 }] },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { x: { beginAtZero: true, grid: { color: "#F1F1F4" } }, y: { grid: { display: false } } },
            },
        });
    }

    renderCharts() {
        const data = this.state.data;
        if (!data) {
            return;
        }
        this._donut(
            this.syncChartRef, "sync",
            [_t("Synced"), _t("Pending"), _t("Error")],
            [data.synced_count, data.pending_count, data.error_count],
            ["#059669", "#94A3B8", "#DC2626"],
        );
        this._donut(
            this.stockChartRef, "stock",
            [_t("In Stock"), _t("Low Stock"), _t("Out of Stock")],
            [data.in_stock_products - data.low_stock_products, data.low_stock_products, data.out_stock_products],
            ["#059669", "#D97706", "#DC2626"],
        );
        this._bar(this.brandChartRef, "brand", data.top_brands.map((b) => b.name), data.top_brands.map((b) => b.count), "#4F46E5");
        this._bar(this.categoryChartRef, "category", data.top_categories.map((c) => c.name), data.top_categories.map((c) => c.count), "#7C3AED");
    }
}

registry.category("actions").add("bigcommerce_catalog_dashboard", BigcommerceCatalogDashboard);
