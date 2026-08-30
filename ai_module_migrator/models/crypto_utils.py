import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from odoo.tools import config

_logger = logging.getLogger(__name__)

TOKEN_PREFIX = "amm1:"  # marks a value as our Fernet ciphertext, vs. legacy plaintext


def _key_path():
    key_dir = os.path.join(config["data_dir"], "ai_module_migrator")
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    return os.path.join(key_dir, "secret.key")


def _get_fernet():
    path = _key_path()
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        _logger.info("Generated new API key encryption key at %s", path)
    return Fernet(key)


def encrypt_secret(plain):
    """Encrypt a secret for storage in ir.config_parameter. Falsy input passes through."""
    if not plain:
        return plain
    token = _get_fernet().encrypt(plain.encode("utf-8")).decode("ascii")
    return TOKEN_PREFIX + token


def decrypt_secret(stored):
    """Decrypt a value previously written by encrypt_secret.

    Values written before this encryption was introduced are plain text
    (no TOKEN_PREFIX) — they are returned unchanged so existing configured
    API keys keep working; they get re-encrypted the next time Settings is
    saved. Anything that fails to decrypt (foreign/corrupted value) is
    likewise returned as-is rather than raising, so a bad ir.config_parameter
    edit can't break every AI call with a traceback.
    """
    if not stored or not stored.startswith(TOKEN_PREFIX):
        return stored
    token = stored[len(TOKEN_PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        _logger.warning("Could not decrypt stored API key value; returning as-is")
        return stored
