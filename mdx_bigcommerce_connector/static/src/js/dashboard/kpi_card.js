import { Component, t, useProps } from "@odoo/owl";

export class BigcommerceKpiCard extends Component {
    static template = "mdx_bigcommerce_connector.KpiCard";
    props = useProps({
        label: t.string(),
        value: t.string(),
        sublabel: t.string().optional(),
        icon: t.string(),
        accent: t.string().optional("primary"),
    });
}
