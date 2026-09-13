{
    # Title measured against the store: proven sellers sit at 22-28 characters
    # with no pipes and roughly a third carry "Odoo". "Credential" and
    # "Encryption" are the nouns someone with this problem actually types.
    "name": "Odoo Credential Encryption",
    "version": "19.0.1.0.0",
    "summary": "Encrypt passwords, API keys and OAuth tokens at rest so a database dump, a "
               "replica or a SQL console shows ciphertext instead of your credentials",
    "description": """
Odoo Credential Encryption
==========================

Odoo stores credentials as ordinary text. The SMTP password, the FTP password a
backup module uses, a connector's API key, an OAuth refresh token - all of them
sit in plain columns that anyone who can read the table can read.

That table gets read more often than people think: a database dump handed to a
developer, a replica pointed at a reporting tool, a restored backup on a laptop,
or a SQL console inside Odoo itself.

This module encrypts chosen fields at rest, transparently.

Pick the fields
    Any stored text field on any model. Point it at the password fields your
    connectors, mail servers and backup tools already use.

Nothing else changes
    Values are encrypted on write and decrypted on read by the ORM itself, so
    the module that owns the field keeps working exactly as before. No code
    changes, no re-entering credentials, no new API to adopt.

Migrate what is already there
    Existing plain-text values stay readable until you choose to encrypt them,
    then one action rewrites the column in place. Re-running it is safe.

Fernet, with the key off the database
    AES-128-CBC with an HMAC, so tampered data fails loudly rather than
    decrypting to nonsense. The key lives in the filesystem data directory,
    never in a table, which is what makes a stolen dump useless.

IMPORTANT: back up the key file alongside your database. It lives in the Odoo
data directory. Lose it and the encrypted credentials cannot be recovered - that
is the property that makes the encryption worth anything, and it cuts both ways.

Scope, stated plainly: this protects data at rest. It is not a permission layer.
A user who can already read the field through Odoo still reads the value, because
masking it would break every integration that legitimately needs it.
    """,
    "category": "Technical",
    "author": "ModuleDex",
    "maintainer": "ModuleDex",
    "license": "OPL-1",
    "price": 89.00,
    "currency": "USD",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "images": ["static/description/banner.png"],
    "depends": ["base"],
    # Ships with Odoo, but declaring it turns a raw ImportError on a slim
    # install into Odoo's clear "missing python library" message.
    "external_dependencies": {"python": ["cryptography"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/vault_field_views.xml",
        "views/vault_menus.xml",
    ],
    "application": True,
    "installable": True,
}
