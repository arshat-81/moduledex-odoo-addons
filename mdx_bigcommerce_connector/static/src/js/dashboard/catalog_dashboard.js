import { Component, onWillStart, proxy, useProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useChart } from "@web/core/utils/chart_hook";
import { Layout } from "@web/search/layout";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";
import { _t } from "@web/core/l10n/translation";
import { BigcommerceKpiCard } from "./kpi_card";

export class BigcommerceCatalogDashboard extends Component {
    static template = "mdx_bigcommerce_connector.CatalogDashboard";
    static components = { Layout, BigcommerceKpiCard };
    props = useProps(standardActionServiceProps);

    setup() {
        this.orm = useService("orm");
        this.display = { controlPanel: {} };

        this.state = proxy({
            configId: this.props.action.context?.default_config_id || false,
            configs: [],
            data: null,
        });

        // useChart loads chart.js, draws once the canvas is in the DOM, redraws
        // after every patch and destroys the chart with the component.
        this.syncChart = useChart(() => {
            const data = this.state.data;
            return this._donut(
                [_t("Synced"), _t("Pending"), _t("Error")],
                [data.synced_count, data.pending_count, data.error_count],
                ["#059669", "#94A3B8", "#DC2626"],
            );
        });
        this.stockChart = useChart(() => {
            const data = this.state.data;
            return this._donut(
                [_t("In Stock"), _t("Low Stock"), _t("Out of Stock")],
                [data.in_stock_products - data.low_stock_products, data.low_stock_products, data.out_stock_products],
                ["#059669", "#D97706", "#DC2626"],
            );
        });
        this.brandChart = useChart(() => {
            const brands = this.state.data.top_brands;
            return this._bar(brands.map((b) => b.name), brands.map((b) => b.count), "#4F46E5");
        });
        this.categoryChart = useChart(() => {
            const categories = this.state.data.top_categories;
            return this._bar(categories.map((c) => c.name), categories.map((c) => c.count), "#7C3AED");
        });

        onWillStart(async () => {
            this.state.configs = await this.orm.searchRead("bigcommerce.config", [], ["id", "name"]);
            await this.loadData();
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

    _donut(labels, values, colors) {
        return {
            type: "doughnut",
            data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, cutout: "68%", plugins: { legend: { position: "bottom" } } },
        };
    }

    _bar(labels, values, color) {
        return {
            type: "bar",
            data: { labels, datasets: [{ label: _t("Products"), data: values, backgroundColor: color, borderRadius: 6, maxBarThickness: 40 }] },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { x: { beginAtZero: true, grid: { color: "#F1F1F4" } }, y: { grid: { display: false } } },
            },
        };
    }
}

registry.category("actions").add("mdx_bigcommerce_catalog_dashboard", BigcommerceCatalogDashboard);
