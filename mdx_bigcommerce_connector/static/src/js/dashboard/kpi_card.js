import { Component } from "@odoo/owl";

export class BigcommerceKpiCard extends Component {
    static template = "mdx_bigcommerce_connector.KpiCard";
    static props = {
        label: String,
        value: String,
        sublabel: { type: String, optional: true },
        icon: String,
        accent: { type: String, optional: true },
    };
    static defaultProps = { accent: "primary" };
}
