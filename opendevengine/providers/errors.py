"""Stable, content-free provider service errors (opendevengine.m1/1).

Every error carries a stable ``code``, an ``http_status`` for the HTTP
adapter and an optional nonsecret ``detail`` dictionary. Messages never contain request
source, response bodies, endpoints with credentials or secret values.
"""
from opendevengine.platform.errors import PlatformError, NotFound


class ProviderError(PlatformError):
    code = 'provider_error'
    http_status = 500
    default_message = 'Provider operation failed.'

    def __init__(self, message=None, *, detail=None):
        super().__init__(message or self.default_message)
        self.detail = dict(detail or {})


class ProviderValidationError(ProviderError, ValueError):
    code = 'validation_failed'
    http_status = 400
    default_message = 'Provider request is invalid.'


class ProfileNotFound(ProviderError, NotFound):
    code = 'not_found'
    http_status = 404
    default_message = 'Provider profile or version not found.'


class ProfileDeleted(ProviderError):
    code = 'profile_deleted'
    http_status = 409
    default_message = 'Provider profile was deleted.'


class ProfileNameConflict(ProviderError):
    code = 'profile_name_conflict'
    http_status = 409
    default_message = 'A provider profile with this name already exists.'


class RoleUnassigned(ProviderError):
    code = 'role_unassigned'
    http_status = 409
    default_message = 'No provider profile is assigned to this role.'


class PersistenceRequired(ProviderError):
    code = 'persistence_required'
    http_status = 409
    default_message = 'Saving credentials requires persistent storage and a secure credential store.'


class CredentialUnavailable(ProviderError):
    code = 'credential_unavailable'
    http_status = 409
    default_message = 'Provider credential is unavailable; reconfigure the profile credential.'


class DestinationNotAllowed(ProviderError):
    code = 'destination_not_allowed'
    http_status = 409
    default_message = 'Local-only mode blocks this provider destination.'


class SourceSharingNotAllowed(ProviderError):
    code = 'source_sharing_not_allowed'
    http_status = 409
    default_message = 'This profile does not allow sending repository source to a remote destination.'


class UnsupportedCapability(ProviderError):
    code = 'unsupported_capability'
    http_status = 409
    default_message = 'The provider profile does not declare the required capability.'


class ProviderAuth(ProviderError):
    code = 'provider_auth'
    http_status = 409
    default_message = 'Provider rejected the credential.'


class ModelNotFound(ProviderError):
    code = 'model_not_found'
    http_status = 409
    default_message = 'Provider could not find the configured model or endpoint.'


class ContextOverflow(ProviderError):
    code = 'context_overflow'
    http_status = 400
    default_message = 'Request exceeds the configured or provider context limit.'


class ProviderRateLimited(ProviderError):
    code = 'provider_rate_limited'
    http_status = 429
    default_message = 'Provider rate limit reached.'

    @property
    def retry_at(self):
        return self.detail.get('retry_at')


class ProviderTimeout(ProviderError):
    code = 'provider_timeout'
    http_status = 504
    default_message = 'Provider request timed out.'


class ProviderUnavailable(ProviderError):
    code = 'provider_unavailable'
    http_status = 503
    default_message = 'Provider is temporarily unavailable.'


class ProviderRejected(ProviderError):
    code = 'provider_rejected'
    http_status = 502
    default_message = 'Provider rejected the request.'


class UpstreamRedirect(ProviderError):
    code = 'upstream_redirect'
    http_status = 502
    default_message = 'Provider attempted a redirect; redirects are not followed.'


class InvalidOutput(ProviderError):
    code = 'invalid_output'
    http_status = 502
    default_message = 'Provider returned malformed output.'


class EmbeddingMismatch(ProviderError):
    code = 'embedding_mismatch'
    http_status = 502
    default_message = 'Provider embeddings do not match the request or configured dimensions.'


class ProviderCancelled(ProviderError):
    code = 'cancelled'
    http_status = 409
    default_message = 'Provider request was cancelled.'


ALL_ERRORS = (
    ProviderValidationError, ProfileNotFound, ProfileDeleted, ProfileNameConflict, RoleUnassigned,
    PersistenceRequired, CredentialUnavailable, DestinationNotAllowed, SourceSharingNotAllowed,
    UnsupportedCapability, ProviderAuth, ModelNotFound, ContextOverflow, ProviderRateLimited,
    ProviderTimeout, ProviderUnavailable, ProviderRejected, UpstreamRedirect, InvalidOutput,
    EmbeddingMismatch, ProviderCancelled,
)
