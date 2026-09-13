{
    "name": "Odoo Performance Auditor",
    "summary": "Find the database, ORM, cron, and configuration problems that make Odoo slow — "
               "ranked findings, a health score, one-click safe fixes, and a client-ready report.",
    "description": "Read-only auditor for self-hosted Odoo. Runs 41 checks against the "
                   "PostgreSQL catalog, the ORM registry, the scheduled-action and mail queues, "
                   "and the server configuration, then produces a ranked list of findings with a "
                   "measured value, a threshold and a recommendation, a weighted 0-100 health "
                   "score, one-click safe fixes (ANALYZE / VACUUM / CREATE INDEX CONCURRENTLY "
                   "only), and a PDF Health Report. Nothing it reads is written; Settings access "
                   "only.",
    "version": "19.0.1.0.1",
    "category": "Technical",
    "author": "ModuleDex",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "license": "OPL-1",
    "price": 99.0,
    "currency": "USD",
    "icon": "/mdx_perf_auditor/static/description/icon.png",
    # Main image on the Apps Store listing. The screenshots live inside
    # index.html, which is the pattern that is known to render on
    # apps.odoo.com - see the other ModuleDex listings.
    "images": ["static/description/banner.png"],
    "depends": ["base", "base_setup", "web", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/perf_config_data.xml",
        "data/ir_cron_data.xml",
        "report/perf_health_report.xml",
        "report/perf_health_report_templates.xml",
        "views/perf_detector_views.xml",
        "views/res_config_settings_views.xml",
        "views/perf_audit_finding_views.xml",
        "views/perf_audit_run_views.xml",
        "views/perf_db_snapshot_views.xml",
        "wizard/perf_model_analysis_views.xml",
        "views/perf_dashboard_action.xml",
        "views/perf_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "mdx_perf_auditor/static/src/dashboard/**/*",
        ],
    },
    "post_init_hook": "post_init_sync",
    "application": True,
    "installable": True,
}
