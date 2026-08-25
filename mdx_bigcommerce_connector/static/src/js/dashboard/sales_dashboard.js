import { Component, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";
import { Layout } from "@web/search/layout";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { _t } from "@web/core/l10n/translation";
import { BigcommerceKpiCard } from "./kpi_card";

const CHART_COLORS = ["#4F46E5", "#059669", "#D97706", "#DC2626", "#0EA5E9", "#7C3AED", "#DB2777", "#65A30D"];

export class BigcommerceSalesDashboard extends Component {
    static template = "mdx_bigcommerce_connector.SalesDashboard";
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

        this.channelChartRef = useRef("channelChart");
        this.cartChartRef = useRef("cartChart");
        this.channelChart = null;
        this.cartChart = null;

        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            this.state.configs = await this.orm.searchRead("bigcommerce.config", [], ["id", "name", "currency_id"]);
            await this.loadData();
        });

        useEffect(() => this.renderCharts());
        onWillUnmount(() => {
            this.channelChart?.destroy();
            this.cartChart?.destroy();
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

    renderCharts() {
        const data = this.state.data;
        if (!data) {
            return;
        }

        this.channelChart?.destroy();
        if (this.channelChartRef.el) {
            const labels = data.per_channel.map((c) => c.name);
            const revenue = data.per_channel.map((c) => c.revenue);
            this.channelChart = new Chart(this.channelChartRef.el, {
                type: "bar",
                data: {
                    labels,
                    datasets: [{
                        label: _t("Revenue"),
                        data: revenue,
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
            });
        }

        this.cartChart?.destroy();
        if (this.cartChartRef.el) {
            const abandoned = data.carts_abandoned;
            const converted = data.carts_converted;
            this.cartChart = new Chart(this.cartChartRef.el, {
                type: "doughnut",
                data: {
                    labels: [_t("Abandoned"), _t("Converted")],
                    datasets: [{
                        data: [abandoned, converted],
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
            });
        }
    }
}

registry.category("actions").add("bigcommerce_sales_dashboard", BigcommerceSalesDashboard);
