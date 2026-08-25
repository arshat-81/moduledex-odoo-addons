{
    "name": "Odoo BigCommerce Connector",
    "summary": "BigCommerce Odoo integration for products, orders, inventory, carts, refunds",
    "description": """
Odoo BigCommerce Connector
==========================
BigCommerce Connector for Odoo 19 by ModuleDex. This BigCommerce Odoo
integration syncs products, variants, categories, customers, sales orders,
inventory, price lists, refunds, abandoned carts, and multi-storefront channels
between BigCommerce and Odoo.

It is built around what BigCommerce's own API actually supports but most
connectors ignore:

* Multi-Storefront/Channel-aware catalog and pricing sync.
* Product, variant, category, customer, sales order, and inventory sync.
* BigCommerce Price Lists synced to Odoo pricelists, tied to Customer Groups.
* Customers and their addresses synced into res.partner.
* Publish Odoo products to BigCommerce as new catalog products.
* Targeted import of specific BigCommerce records by id, id range, or all.
* Refunds pulled in, optionally raising an Odoo credit note automatically.
* Abandoned cart tracking with one-click conversion into a CRM lead.
* Webhook-driven real-time sync (fetch-on-receipt, since BigCommerce webhook
  payloads only carry a resource id) backed by a cron fallback.
* Rate-limit-aware API client honoring BigCommerce's backoff headers.
* Per-record audit log with before/after values, plus sales and catalog dashboards.

See static/description/index.html for full setup instructions.
""",
    "category": "Sales/Sales",
    "version": "19.0.1.1.0",
    "license": "OPL-1",
    "author": "ModuleDex",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "price": 249.0,
    "currency": "USD",
    "icon": "/mdx_bigcommerce_connector/static/description/icon.png",
    "images": ["static/description/banner.png"],
    "depends": ["base_import", "sale_management", "stock", "account", "crm"],
    "data": [
        "security/bigcommerce_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "data/bigcommerce_dashboard_actions.xml",
        "views/bigcommerce_config_views.xml",
        "views/bigcommerce_channel_views.xml",
        "views/bigcommerce_category_views.xml",
        "views/bigcommerce_customer_group_views.xml",
        "views/bigcommerce_price_list_views.xml",
        "views/bigcommerce_product_views.xml",
        "views/bigcommerce_cart_views.xml",
        "views/bigcommerce_refund_views.xml",
        "views/bigcommerce_webhook_views.xml",
        "views/bigcommerce_update_log_views.xml",
        "views/sale_order_views.xml",
        "views/res_partner_views.xml",
        "wizard/bigcommerce_sync_wizard_views.xml",
        "wizard/bigcommerce_import_wizard_views.xml",
        "wizard/bigcommerce_bulk_update_wizard_views.xml",
        "wizard/bigcommerce_publish_wizard_views.xml",
        "views/bigcommerce_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "mdx_bigcommerce_connector/static/src/scss/bigcommerce_dashboard.scss",
            "mdx_bigcommerce_connector/static/src/js/dashboard/kpi_card.js",
            "mdx_bigcommerce_connector/static/src/js/dashboard/sales_dashboard.js",
            "mdx_bigcommerce_connector/static/src/js/dashboard/catalog_dashboard.js",
            "mdx_bigcommerce_connector/static/src/xml/dashboard/kpi_card.xml",
            "mdx_bigcommerce_connector/static/src/xml/dashboard/sales_dashboard.xml",
            "mdx_bigcommerce_connector/static/src/xml/dashboard/catalog_dashboard.xml",
        ],
    },
    "application": True,
    "installable": True,
}
