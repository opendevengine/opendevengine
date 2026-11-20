"""M0 platform foundation public interface."""
CONTRACT = 'opendevengine.m0/1'
from .clock import now_ms, format_ts, new_id
from .errors import (PlatformError, StorageBusy, SchemaTooNew, MigrationChecksumMismatch,
                     MigrationFailed, NotFound, InvalidTransition, IdempotencyConflict,
                     LeaseLost, SecretStoreUnavailable, SecretRevoked, SecretMissing)
from .db import Store, DB_FILENAME
from .platform import Platform
from .jobs import (JobQueue, Job, JobState, Lease, HeartbeatResult, TERMINAL,
                   DEFAULT_LEASE_MS, BACKOFF_BASE_MS, BACKOFF_CAP_MS)
from .secrets import (SecretService, SecretBackend, SecretPurpose, BackendStatus,
                      UnavailableSecretStore, SecretRef, SecretValue)
from .worker import Worker, JobContext, Handler, RetryableError, PermanentError, UnknownOutcomeError, CancelledError
from .workspace import build_workspace_summary, build_health
