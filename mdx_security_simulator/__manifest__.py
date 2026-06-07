{
    "name": "Security Simulator",
    "summary": "Simulate user access rights, record rules, groups, and field restrictions",
    "version": "17.0.1.0.0",
    "category": "Technical",
    "author": "ModuleDex",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "views/mdx_security_simulator_views.xml",
    ],
    "installable": True,
    "application": False,
}
