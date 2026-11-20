"""Content-free service failures safe for the local HTTP adapter."""
from opendevengine.platform.errors import PlatformError


class GitHubError(PlatformError):
    def __init__(self, code, *, retry_at=None):
        self.code = code
        self.retry_at = retry_at
        super().__init__('GitHub operation failed: ' + code)
