"""Key management and symmetric encryption for stored credentials.

The threat this addresses is a credential readable *at rest*: a database dump
handed to a developer, a replica a reporting tool can query, a SQL console, or a
backup that ends up somewhere it should not. Odoo stores API keys, FTP and SMTP
passwords and OAuth refresh tokens as plain strings in ordinary columns, so
anyone who can read the table reads the secret.

Design decisions worth stating, because each one is a trade-off:

* **Fernet (AES-128-CBC + HMAC-SHA256).** Authenticated encryption, so a
  tampered ciphertext fails loudly instead of decrypting to garbage.
* **The key lives on the filesystem, never in the database.** A dump of the
  database therefore contains no way to decrypt itself, which is the entire
  point. It does mean the key must be part of your backup routine - losing it
  loses the secrets, and the module says so in its description rather than
  burying it.
* **A marker prefix** distinguishes our ciphertext from a value that was already
  there in plain text. Anything without the marker is returned untouched, so
  enabling protection on a populated field cannot break a running integration;
  the values are migrated deliberately, not implicitly.
"""

import base64
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from odoo import _
from odoo.exceptions import UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)

# Marks a value as ciphertext produced by this module. Chosen to be something no
# realistic credential starts with, and versioned so a future scheme can coexist.
TOKEN_PREFIX = "mdxv1:"

_KEY_DIRNAME = "mdx_credential_vault"
_KEY_FILENAME = "vault.key"


def key_path():
    """Absolute path of the key file, inside Odoo's data_dir."""
    directory = os.path.join(config["data_dir"], _KEY_DIRNAME)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return os.path.join(directory, _KEY_FILENAME)


def key_exists():
    return os.path.exists(key_path())


def _load_or_create_key():
    """Return the Fernet key, creating it on first use.

    Created with O_EXCL so two workers racing on first use cannot each write a
    key and leave one of them unable to read what the other encrypted, and with
    mode 0600 so it is not world-readable on a shared host.
    """
    path = key_path()
    if os.path.exists(path):
        with open(path, "rb") as handle:
            return handle.read().strip()

    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # another worker won the race; use theirs
        with open(path, "rb") as handle:
            return handle.read().strip()
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    _logger.info("mdx_credential_vault: created a new encryption key at %s", path)
    return key


def _fernet():
    try:
        return Fernet(_load_or_create_key())
    except Exception as error:
        raise UserError(_(
            "The credential vault key at %(path)s could not be read (%(error)s). "
            "Restore it from your backup: without it the stored credentials cannot "
            "be decrypted.",
            path=key_path(), error=error,
        )) from error


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(TOKEN_PREFIX)


def encrypt(value):
    """Encrypt a string. Already-encrypted and empty values pass through."""
    if not value or not isinstance(value, str) or is_encrypted(value):
        return value
    token = _fernet().encrypt(value.encode("utf-8"))
    return TOKEN_PREFIX + base64.urlsafe_b64encode(token).decode("ascii")


def decrypt(value):
    """Decrypt a value produced by :func:`encrypt`.

    A value without the marker is returned unchanged: it was stored before the
    field was protected, and refusing to return it would break the integration
    that depends on it.
    """
    if not is_encrypted(value):
        return value
    payload = value[len(TOKEN_PREFIX):]
    try:
        token = base64.urlsafe_b64decode(payload.encode("ascii"))
        return _fernet().decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        # Wrong key, or the column was edited by hand. Returning the raw value
        # would leak ciphertext into a UI field and invite someone to "fix" it
        # by overwriting a secret they cannot read.
        _logger.warning("mdx_credential_vault: a stored value could not be decrypted; "
                        "the key may have changed")
        raise UserError(_(
            "A stored credential could not be decrypted. This usually means the vault "
            "key was replaced or restored from a different server. Restore the original "
            "key file, or clear and re-enter the credential."
        ))
