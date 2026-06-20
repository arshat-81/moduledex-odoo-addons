{
    "name": "SQL Query & Report Builder",
    "summary": "Safe SQL query runner with report previews, CSV export, execution history, and admin full-access control.",
    "description": """
Data Insight Workbench is the full edition SQL workspace for Odoo administrators
and trusted power users. It combines saved SQL assets, tutorial queries,
parameter support, Run and Explain execution, localized execution history,
result previews, CSV exports, raw JSON, and explicit full-access control for
live database changes.

Administrators can keep regular users behind a read-only SQL guard while
granting full SQL access only to selected users who need CREATE, UPDATE, DELETE,
DROP, and other maintenance statements.
    """,
    "version": "17.0.1.0.0",
    "category": "Technical",
    "author": "Moduledex",
    "license": "OPL-1",
    "price": 59.0,
    "currency": "USD",
    "icon": "/data_insight_workbench/static/description/icon.png",
    "images": [
        "static/description/banner.png",
        "static/description/screenshots/app_overview.png",
        "static/description/screenshots/getting_started_queries.png",
        "static/description/screenshots/query_form.png",
        "static/description/screenshots/run_wizard.png",
        "static/description/screenshots/execution_result.png",
    ],
    "depends": ["base", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/res_users_views.xml",
        "views/data_insight_query_views.xml",
        "views/data_insight_execution_views.xml",
        "wizard/data_insight_query_run_wizard_views.xml",
        "data/query_templates.xml",
    ],
    "application": True,
    "installable": True,
}
