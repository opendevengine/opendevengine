"""Content-free session failures. ``code`` is stable and safe for HTTP mapping."""
from opendevengine.platform.errors import PlatformError


class SessionError(PlatformError):
    """Root maps codes to HTTP: validation->400, not_found->404, conflict/idempotency_conflict/
    plan_not_ready/session_active/runtime_in_use/retry_exists->409, source_unavailable->403."""

    def __init__(self, code, *, details=None):
        self.code = code
        self.details = list(details or ())
        super().__init__('Session operation failed: ' + code)
