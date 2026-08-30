{
    # Display name only — the technical name stays ai_module_migrator, so no
    # XML ids, asset paths or installed databases are affected. "Addon" rather
    # than "Module" keeps this clear of OCA's free odoo-module-migrator library,
    # which a paid listing should not be confused with. Both "module" and
    # "addon" are kept in the summary so store search matches either word.
    "name": "AI Addon Migrator",
    "version": "19.0.1.0.26",
    "summary": "AI migration tool for custom Odoo modules and addons — upgrade or "
               "downgrade any addon between Odoo 11 and 19",
    "description": """
AI Addon Migrator
=================

Scan an Odoo addon from a server path or uploaded zip, compare its dependencies
against local Community and Enterprise addon roots, and generate an AI-assisted
migration report for any Odoo 11-19 source/target version pair.

Long-running AI work (plan generation and per-file code migration) runs in the
background through this module's own queue, driven by a standard Odoo scheduled
action. No external queue module and no odoo.conf changes are required.

IMPORTANT: because AI calls can run for minutes, raise the cron watchdog in
odoo.conf or the server will restart mid-migration::

    limit_time_real_cron = 3600

On-premise/self-hosted only (not compatible with Odoo Online). Source code is
sent to whichever third-party AI provider you configure; Settings requires an
explicit opt-in before any such call is made.
    """,
    "category": "Technical",
    "author": "ModuleDex",
    "maintainer": "ModuleDex",
    # Paid apps on the Odoo Apps Store must ship under the Odoo Proprietary
    # License; LGPL-3 cannot be sold there.
    "license": "OPL-1",
    "price": 149.00,
    "currency": "USD",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "images": ["static/description/banner.png"],
    "depends": ["base", "mail", "web"],
    # Ships with Odoo (requirements.txt), but declaring it turns a raw
    # ImportError on a slim/custom install into Odoo's clear "missing python
    # library" message. Used by crypto_utils to encrypt stored API keys.
    "external_dependencies": {"python": ["cryptography"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/res_config_settings_views.xml",
        # module_migration_job_views.xml defines the root menu that
        # migration_queue_views.xml hangs its menu item from, so it must load first.
        "views/module_migration_job_views.xml",
        "views/migration_queue_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ai_module_migrator/static/src/js/auto_refresh_widget.js",
            "ai_module_migrator/static/src/xml/auto_refresh_widget.xml",
            "ai_module_migrator/static/src/scss/terminal_log.scss",
        ],
    },
    "application": True,
    "installable": True,
}
