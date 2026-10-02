import { Component, onWillStart, proxy, useProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useChart } from "@web/core/utils/chart_hook";
import { Layout } from "@web/search/layout";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";
import { _t } from "@web/core/l10n/translation";
import { BigcommerceKpiCard } from "./kpi_card";

export class BigcommerceSalesDashboard extends Component {
    static template = "mdx_bigcommerce_connector.SalesDashboard";
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
        this.channelChart = useChart(() => this.channelChartConfig());
        this.cartChart = useChart(() => this.cartChartConfig());

        onWillStart(async () => {
            this.state.configs = await this.orm.searchRead("bigcommerce.config", [], ["id", "name", "currency_id"]);
            await this.loadData();
        });
    }

    async loadData() {
        this.state.data = await this.orm.call(
            "bigcommerce.dashboard", "get_dashboard_data", [],
            { config_id: this.state.configId || undefined },
        );
    }

    async onConfigChange(ev) {
        this.state.configId = ev.target.value ? parseInt(ev.target.value, 10) : false;
        await this.loadData();
    }

    formatMoney(value) {
        return new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value || 0);
    }

    channelChartConfig() {
        const data = this.state.data;
        return {
            type: "bar",
            data: {
                labels: data.per_channel.map((c) => c.name),
                datasets: [{
                    label: _t("Revenue"),
                    data: data.per_channel.map((c) => c.revenue),
                    backgroundColor: "#4F46E5",
                    borderRadius: 6,
                    maxBarThickness: 48,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { beginAtZero: true, grid: { color: "#F1F1F4" } },
                    x: { grid: { display: false } },
                },
            },
        };
    }

    cartChartConfig() {
        const data = this.state.data;
        return {
            type: "doughnut",
            data: {
                labels: [_t("Abandoned"), _t("Converted")],
                datasets: [{
                    data: [data.carts_abandoned, data.carts_converted],
                    backgroundColor: ["#D97706", "#059669"],
                    borderWidth: 0,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "68%",
                plugins: { legend: { position: "bottom" } },
            },
        };
    }
}

registry.category("actions").add("mdx_bigcommerce_sales_dashboard", BigcommerceSalesDashboard);
