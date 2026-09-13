{
    # Display name only — the technical name stays ai_module_migrator, so no
    # XML ids, asset paths, ir.config_parameter keys or installed databases are
    # affected by a retitle.
    #
    # Title rationale, measured against the store rather than guessed:
    #   - "Odoo" appears in 32-35% of titles that sell (500+ sold, or 10+/month,
    #     or 5,000+ downloads) and in none of the low-selling ones.
    #   - 22 characters matches the 22-23 char median of those same sellers.
    #     Pipe-stuffed keyword titles correlate with NOT selling: 0% of apps
    #     with 500+ sales carry two or more pipes, against 17% of apps with
    #     1-19 sales.
    #   - "Module" is the word buyers type; "addon" is not.
    #   - "Upgrade" rather than "Migration" keeps a paid listing clear of OCA's
    #     free odoo-module-migrator CLI, which this should not be mistaken for.
    #     "migration", "addon" and "downgrade" all stay in the summary below, so
    #     store search still matches every one of those words.
    "name": "Odoo Module Upgrade AI",
    "version": "19.0.1.1.0",
    "summary": "AI migration tool for custom Odoo modules and addons — upgrade or "
               "downgrade any addon between Odoo 11 and 20",
    "description": """
Odoo Module Upgrade AI
======================

Scan an Odoo addon from a server path or uploaded zip, compare its dependencies
against local Community and Enterprise addon roots, and generate an AI-assisted
migration report for any Odoo 11-20 source/target version pair.

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
