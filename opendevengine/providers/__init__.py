"""Model provider profiles and inference adapters."""
CONTRACT = 'opendevengine.m1/1'

from .adapters import ADAPTER_IMPLEMENTATIONS, OllamaAdapter, OpenAICompatibleAdapter
from .errors import (ALL_ERRORS, ContextOverflow, CredentialUnavailable, DestinationNotAllowed,
                     EmbeddingMismatch, InvalidOutput, ModelNotFound, PersistenceRequired,
                     ProfileDeleted, ProfileNameConflict, ProfileNotFound, ProviderAuth,
                     ProviderCancelled, ProviderError, ProviderRateLimited, ProviderRejected,
                     ProviderTimeout, ProviderUnavailable, ProviderValidationError, RoleUnassigned,
                     SourceSharingNotAllowed, UnsupportedCapability, UpstreamRedirect)
from .service import ProviderService
from .transport import (ResponseTooLarge, TransportCancelled, TransportFailure, TransportRequest,
                        TransportResponse, TransportTimeout, TransportUnavailable, UrllibTransport)
from .validation import ADAPTERS, CAPABILITIES, ROLES, is_loopback_host, validate_endpoint

__all__ = [name for name in dir() if not name.startswith('_')]
