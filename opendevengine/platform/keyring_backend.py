"""Explicit OS credential vault selection; no third-party discovery or plaintext fallback."""
import base64
import importlib
import sys

from .errors import SecretStoreUnavailable
from .secrets import BackendStatus


class OSKeyringBackend:
    """Use only the platform's named secure backend, independent of keyring config."""

    SERVICE = "opendevengine.local"
    BACKENDS = {
        "darwin": ("keyring.backends.macOS", "Keyring"),
        "linux": ("keyring.backends.SecretService", "Keyring"),
        "win32": ("keyring.backends.Windows", "WinVaultKeyring"),
    }

    def __init__(self, *, platform_name=None, module_loader=importlib.import_module):
        self.name = "os_keyring"
        self._backend = None
        self._missing_error = ()
        platform_name = platform_name or sys.platform
        spec = self.BACKENDS.get(platform_name)
        if spec is not None:
            try:
                backend = getattr(module_loader(spec[0]), spec[1])()
                if backend.priority > 0:
                    self._backend = backend
                    self._missing_error = module_loader("keyring.errors").PasswordDeleteError
            except Exception:
                self._backend = None

    def status(self):
        available = self._backend is not None
        return BackendStatus(self.name, available, None if available else
                             "Install the credentials extra and unlock the supported OS credential vault. Credential setup is disabled until then.")

    def _call(self, method, *args):
        if self._backend is None:
            raise SecretStoreUnavailable("No supported secure credential vault is available.")
        failed = False
        try:
            result = getattr(self._backend, method)(self.SERVICE, *args)
        except Exception:
            failed = True
        # Raise after leaving the except block: backend exceptions may contain values.
        if failed:
            raise SecretStoreUnavailable("Secure credential vault operation failed.") from None
        return result

    def put(self, ref_id, value):
        if not isinstance(value, bytes) or not value or len(value) > 128 * 1024:
            raise ValueError("Credential value must be nonempty bounded bytes.")
        self._call("set_password", ref_id, base64.b64encode(value).decode("ascii"))

    def get(self, ref_id):
        value = self._call("get_password", ref_id)
        if value is None:
            raise KeyError("Credential is unavailable in this vault.")
        failed = False
        try:
            result = base64.b64decode(value, validate=True)
        except Exception:
            failed = True
        if failed or not result or len(result) > 128 * 1024:
            raise SecretStoreUnavailable("Stored credential format is invalid.") from None
        return result

    def delete(self, ref_id):
        if self._backend is None:
            raise SecretStoreUnavailable("No supported secure credential vault is available.")
        failed = False
        try:
            if self._backend.get_password(self.SERVICE, ref_id) is None:
                return
            self._backend.delete_password(self.SERVICE, ref_id)
        except self._missing_error:
            return
        except Exception:
            failed = True
        if failed:
            raise SecretStoreUnavailable("Secure credential vault deletion failed.") from None
