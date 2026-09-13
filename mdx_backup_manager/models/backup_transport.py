"""Where a backup is written, and how it is read back.

Each transport implements three operations - ``put``, ``fetch`` and ``list`` -
and nothing else knows how they work. ``fetch`` is the one that matters and the
reason this is a separate layer: a backup you cannot read back is not a backup,
so every destination has to be able to return what it stored.

Only transports that need no third-party package are always available. SFTP is
offered when ``paramiko`` is importable and refuses clearly when it is not,
rather than being hidden - an administrator looking for SFTP should find out why
it is unavailable, not wonder whether the module supports it.
"""

import ftplib
import io
import logging
import os
import shutil

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:  # optional, and deliberately not a hard dependency
    import paramiko
except ImportError:
    paramiko = None


class Transport:
    """Base class. ``destination`` is an ``mdx.backup.destination`` record."""

    def __init__(self, destination):
        self.destination = destination

    # -- interface ----------------------------------------------------
    def put(self, filename, data):
        raise NotImplementedError

    def fetch(self, filename):
        """Return the bytes previously stored under ``filename``."""
        raise NotImplementedError

    def list(self):
        """Return ``[(filename, modified_epoch_or_None)]``."""
        raise NotImplementedError

    def remove(self, filename):
        raise NotImplementedError

    def check(self):
        """Raise UserError if the destination is not usable."""
        raise NotImplementedError


class LocalTransport(Transport):
    """A directory on the Odoo server, or anything mounted into it."""

    @property
    def _root(self):
        path = (self.destination.local_path or "").strip()
        if not path:
            raise UserError(_("This destination has no folder configured."))
        return path

    def check(self):
        root = self._root
        if not os.path.isdir(root):
            raise UserError(_("%s is not a directory on the Odoo server.", root))
        if not os.access(root, os.W_OK):
            raise UserError(_("%s is not writable by the Odoo process.", root))

    def put(self, filename, data):
        self.check()
        with open(os.path.join(self._root, filename), "wb") as handle:
            handle.write(data)

    def fetch(self, filename):
        with open(os.path.join(self._root, filename), "rb") as handle:
            return handle.read()

    def list(self):
        root = self._root
        if not os.path.isdir(root):
            return []
        out = []
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isfile(full):
                out.append((name, os.path.getmtime(full)))
        return out

    def remove(self, filename):
        full = os.path.join(self._root, filename)
        if os.path.exists(full):
            os.remove(full)

    def free_space(self):
        try:
            return shutil.disk_usage(self._root).free
        except OSError:
            return None


class FtpTransport(Transport):
    """Plain FTP, or FTPS when the destination asks for TLS."""

    def _connect(self):
        dest = self.destination
        if not dest.host:
            raise UserError(_("This destination has no host configured."))
        cls = ftplib.FTP_TLS if dest.use_tls else ftplib.FTP
        try:
            client = cls(timeout=dest.timeout or 60)
            client.connect(dest.host, dest.port or 21)
            client.login(dest.username or "anonymous", dest.password or "")
            if dest.use_tls:
                client.prot_p()
            if dest.remote_path:
                client.cwd(dest.remote_path)
        except (ftplib.all_errors, OSError) as error:
            raise UserError(_("Could not connect to %(host)s: %(error)s",
                              host=dest.host, error=error)) from error
        return client

    def check(self):
        client = self._connect()
        try:
            client.voidcmd("NOOP")
        finally:
            try:
                client.quit()
            except Exception:
                client.close()

    def put(self, filename, data):
        client = self._connect()
        try:
            client.storbinary("STOR %s" % filename, io.BytesIO(data))
        finally:
            try:
                client.quit()
            except Exception:
                client.close()

    def fetch(self, filename):
        client = self._connect()
        buffer = io.BytesIO()
        try:
            client.retrbinary("RETR %s" % filename, buffer.write)
        finally:
            try:
                client.quit()
            except Exception:
                client.close()
        return buffer.getvalue()

    def list(self):
        client = self._connect()
        try:
            names = client.nlst()
        except ftplib.error_perm:
            names = []
        finally:
            try:
                client.quit()
            except Exception:
                client.close()
        return [(name, None) for name in names]

    def remove(self, filename):
        client = self._connect()
        try:
            client.delete(filename)
        except ftplib.error_perm:
            pass
        finally:
            try:
                client.quit()
            except Exception:
                client.close()


class SftpTransport(Transport):
    """SFTP, when paramiko is available."""

    def _client(self):
        if paramiko is None:
            raise UserError(_(
                "SFTP needs the 'paramiko' Python package, which is not installed on this "
                "server. Install it (pip install paramiko) and restart Odoo, or use a "
                "different destination type."))
        dest = self.destination
        transport = paramiko.Transport((dest.host, dest.port or 22))
        transport.connect(username=dest.username or "", password=dest.password or "")
        return paramiko.SFTPClient.from_transport(transport), transport

    def check(self):
        client, transport = self._client()
        try:
            client.listdir(self.destination.remote_path or ".")
        finally:
            client.close()
            transport.close()

    def _path(self, filename):
        root = self.destination.remote_path or "."
        return "%s/%s" % (root.rstrip("/"), filename)

    def put(self, filename, data):
        client, transport = self._client()
        try:
            with client.open(self._path(filename), "wb") as handle:
                handle.write(data)
        finally:
            client.close()
            transport.close()

    def fetch(self, filename):
        client, transport = self._client()
        try:
            with client.open(self._path(filename), "rb") as handle:
                return handle.read()
        finally:
            client.close()
            transport.close()

    def list(self):
        client, transport = self._client()
        try:
            root = self.destination.remote_path or "."
            return [(entry.filename, entry.st_mtime) for entry in client.listdir_attr(root)]
        finally:
            client.close()
            transport.close()

    def remove(self, filename):
        client, transport = self._client()
        try:
            client.remove(self._path(filename))
        except IOError:
            pass
        finally:
            client.close()
            transport.close()


TRANSPORTS = {
    "local": LocalTransport,
    "ftp": FtpTransport,
    "sftp": SftpTransport,
}


def get_transport(destination):
    transport = TRANSPORTS.get(destination.destination_type)
    if transport is None:
        raise UserError(_("Unknown destination type %s.", destination.destination_type))
    return transport(destination)


def sftp_available():
    return paramiko is not None
