from . import models


def post_init_hook(env):
    """Ask the credential vault to encrypt this module's stored passwords.

    Done here rather than in a data file because the vault registry is keyed on
    ir.model.fields rows, which only exist once this module's tables have been
    created.
    """
    env["mdx.backup.destination"]._register_encrypted_fields()
