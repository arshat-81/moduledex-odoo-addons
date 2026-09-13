{
    # The search term everyone will type the week 20.0 lands is "Odoo 20".
    # Second is "upgrade". The name is 23 characters, which is where the
    # listings that actually sell sit.
    "name": "Odoo 20 Upgrade Scanner",
    "version": "19.0.1.0.0",
    "summary": "Find the deprecated code in your custom modules before the Odoo 20 "
               "upgrade does - every finding with a file, a line and the replacement",
    "description": """
Odoo 20 Upgrade Scanner
=======================

The upgrade breaks on code nobody remembered writing. This finds it first.

A migration and compatibility check for your own code: it scans the source of
your modules for APIs Odoo has deprecated or already removed, and reports each
one with the file, the line, the code, and what to write instead. Useful whether
you are moving from 17.0 or 18.0 to 19.0 today, or getting ready for 20.0.

Rules taken from the Odoo source, not from guesswork
    Every rule cites the file and line in Odoo where that deprecation is
    declared, so you can check it yourself. Nothing here is invented.

Graded by when it actually breaks
    Odoo removes a deprecated API one to two versions after marking it. Anything
    deprecated in 18.0 and still present in 19.0 is therefore in its removal
    window now, and is reported as such - separately from things only just
    deprecated in 19.0, and from things already removed.

It reads your code properly
    Python is parsed with the abstract syntax tree, not searched with grep, so a
    deprecated name in a comment, a docstring or a string literal is not a
    finding. XML is parsed too, and reports the real line number.

Your own rules, too
    Every rule is a record. Add patterns for your own conventions, switch off
    ones you do not care about, and re-run.

Scans custom modules by default
    Odoo's own addons are excluded unless you ask for them - the point is the
    code you have to fix.

Run it before the module installs
    A module that will not load on the new version cannot be inspected as an
    installed module, which is exactly when you need to know what is wrong with
    it. The scanner reads whatever is in the addons path, installed or not.

Free, and it reads only. Nothing is uploaded, nothing is rewritten.
    """,
    "category": "Technical",
    "author": "ModuleDex",
    "maintainer": "ModuleDex",
    "license": "LGPL-3",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "images": ["static/description/banner.png"],
    "depends": ["base"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/upgrade_rule_data.xml",
        "views/upgrade_rule_views.xml",
        "views/upgrade_finding_views.xml",
        "views/upgrade_scan_views.xml",
        "views/upgrade_menus.xml",
    ],
    "application": True,
    "installable": True,
}
