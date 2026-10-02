{
    "name": "Odoo User Access Control",
    "version": "20.0.1.0.0",
    "summary": "Restrict what users can see and do without code: hide menus, fields, buttons "
               "and tabs, make fields read-only, block create, edit and delete, filter records "
               "- with a preview before you enforce",
    "description": """
Odoo User Access Control
========================

Decide what each user or group can see and do, without writing a line of code
and without touching the groups Odoo ships with.

Access profiles
    Collect the restrictions for a role in one place and assign the profile to
    users, to groups, or both. Administrators are left out unless you say
    otherwise, so a mistake cannot lock you out of your own database.

Models and records
    Block create, edit or delete on any model, and limit which records a
    profile can read, edit or delete with an ordinary domain.

    These are written as native Odoo 20 access restrictions. The server
    enforces them everywhere - the web client, the API, exports, scheduled
    actions - and they are listed in Odoo's own Access screen, where you can
    read exactly what was created. Nothing is patched to make them work.

Fields
    Hide a field, make it read-only or make it required, in every view of the
    model including embedded lists. A hidden field also disappears from
    filters, group-by, sorting and the export list. Optionally reject writes to
    a read-only field on the server as well.

Buttons, tabs, chatter, reports and actions
    Hide them per model. A picker reads the model's own views and lists what is
    there, so you choose from names instead of guessing them.

Menus
    Hide a menu and everything under it.

Preview before you enforce
    See what a specific user would lose - which menus, which operations, how
    many records - while the profile is still a draft. Enforce it when the
    answer is the one you wanted, suspend it again in one click.

Scope, stated plainly: hiding a field, button or menu changes the interface.
It does not, on its own, stop a determined user from reaching the same data
through the API. Model and record restrictions do, because the server enforces
them. Use those for anything that has to hold.
    """,
    "category": "Technical",
    "author": "ModuleDex",
    "maintainer": "ModuleDex",
    "license": "OPL-1",
    "price": 249.00,
    "currency": "USD",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "images": [
        "static/description/banner.png",
        "static/description/11_sales_order_restricted_user.png",
        "static/description/02_profile_models_and_records.png",
        "static/description/05_preview_before_enforcing.png",
        "static/description/08_blocked_attempts.png",
    ],
    "depends": ["base", "web", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.access.csv",
        "data/ir_cron_data.xml",
        "wizard/access_preview_views.xml",
        "wizard/element_picker_views.xml",
        "views/access_profile_views.xml",
        "views/access_denial_views.xml",
        "views/access_menus.xml",
    ],
    "application": True,
    "installable": True,
}
