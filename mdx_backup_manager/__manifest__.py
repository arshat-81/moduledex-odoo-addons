{
    # The category is the most contested phrase on the store - 31 backup apps,
    # and the free leader has 26,000 downloads. Competing on the words
    # "database backup" is lost before it starts, so the title leads with the
    # thing none of them do: proving the backup is restorable.
    "name": "Odoo Verified Database Backup",
    "version": "19.0.1.0.0",
    "summary": "Scheduled database backups that are verified after upload, keep their "
               "credentials encrypted, and alert you when a backup stops happening",
    "description": """
Odoo Verified Database Backup
=============================

Most backup modules tell you a backup ran. That is not the same as having a
backup. This one checks.

Verified, not assumed
    After the dump is uploaded it is read back from the destination, its
    SHA-256 compared against what was sent, and the archive opened to confirm
    it really contains dump.sql and manifest.json. A run is only marked
    Verified when the file that came back is the file that went out.

Alerts when backups STOP
    Success mail trains people to ignore success mail. This notifies on
    failure, and separately when a destination has had no successful backup
    for longer than you allow - the silence that actually costs you data.

Credentials encrypted at rest
    FTP and SFTP passwords are stored encrypted rather than as plain strings,
    so a database dump does not hand over the credentials to your other
    backups. Requires Odoo Credential Encryption, which does the work.

Retention that runs
    Keep the last N, or the last N days, applied per destination after each
    successful run.

Destinations
    Local or mounted filesystem and FTP / FTPS out of the box, with no extra
    Python packages. SFTP is available when paramiko is installed; the
    destination type simply refuses to run and says so when it is not.

Backups are written with Odoo's own dump API, the same one behind Database
Manager, so what you get is a standard Odoo archive that restores the standard
way - no proprietary format, and nothing to reverse-engineer if you ever stop
using this module.
    """,
    "category": "Technical",
    "author": "ModuleDex",
    "maintainer": "ModuleDex",
    "license": "OPL-1",
    "price": 129.00,
    "currency": "USD",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "images": ["static/description/banner.png"],
    # mdx_credential_vault is what keeps the stored passwords out of a dump.
    "depends": ["base", "mail", "mdx_credential_vault"],
    "external_dependencies": {"python": []},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/backup_cron.xml",
        "views/backup_destination_views.xml",
        "views/backup_run_views.xml",
        "views/backup_menus.xml",
    ],
    "post_init_hook": "post_init_hook",
    "application": True,
    "installable": True,
}
