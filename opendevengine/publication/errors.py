"""Content-free publication failures safe for the HTTP boundary."""

from opendevengine.platform.errors import PlatformError


class PublicationError(PlatformError):
    def __init__(self, code, *, details=None):
        self.code = code
        self.details = list(details or ())
        self.http_status = 404 if code == 'not_found' else 400 if code == 'validation' else 409
        super().__init__('Publication operation failed.')
