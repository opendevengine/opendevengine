"""Content-free platform failures."""
class PlatformError(Exception):
    code = "internal"

class StorageBusy(PlatformError):
    code = "storage_busy"

class SchemaTooNew(PlatformError):
    code = "schema_too_new"

class MigrationChecksumMismatch(PlatformError):
    code = "migration_checksum_mismatch"

class MigrationFailed(PlatformError):
    code = "migration_failed"

class NotFound(PlatformError):
    code = "not_found"

class InvalidTransition(PlatformError):
    code = "conflict"

class IdempotencyConflict(PlatformError):
    code = "idempotency_conflict"

class LeaseLost(PlatformError):
    code = "lease_lost"

class SecretStoreUnavailable(PlatformError):
    code = "secret_store_unavailable"

class SecretRevoked(PlatformError):
    code = "secret_revoked"

class SecretMissing(PlatformError):
    code = "secret_missing"
