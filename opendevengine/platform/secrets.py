"""Secret references only; M0 has no available runtime credential backend."""
from dataclasses import dataclass
from typing import Protocol, Literal
from .clock import now_ms, new_id
from .errors import NotFound, SecretStoreUnavailable, SecretRevoked, SecretMissing

SecretPurpose = Literal['provider_api_key', 'github_app_private_key', 'github_token']

@dataclass(frozen=True)
class BackendStatus:
    backend: str
    available: bool
    reason: str | None

class SecretBackend(Protocol):
    name: str
    def status(self) -> BackendStatus: ...
    def put(self, ref_id: str, value: bytes) -> None: ...
    def get(self, ref_id: str) -> bytes: ...
    def delete(self, ref_id: str) -> None: ...

class UnavailableSecretStore:
    name = 'unavailable'

    def status(self):
        return BackendStatus(self.name, False, 'No secure credential store is configured. Credential setup is disabled.')

    def put(self, ref_id, value):
        raise SecretStoreUnavailable('Secure credential storage is unavailable.')

    def get(self, ref_id):
        raise SecretStoreUnavailable('Secure credential storage is unavailable.')

    def delete(self, ref_id):
        raise SecretStoreUnavailable('Secure credential storage is unavailable.')

@dataclass(frozen=True)
class SecretRef:
    id: str
    backend: str
    purpose: SecretPurpose
    label: str
    created_at: int
    revoked_at: int | None

class SecretValue:
    def __init__(self, value):
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return 'SecretValue(***)'

    __str__ = __repr__


class SecretService:
    def __init__(self, store, backend, *, clock=now_ms):
        self.store, self.backend, self.clock = store, backend, clock

    def status(self):
        with self.store.read():
            try:
                status = self.backend.status()
            except Exception:
                pass
            else:
                return status
            raise SecretStoreUnavailable('Unable to inspect secure credential storage.')

    def _available(self):
        try:
            available = self.backend.status().available
        except Exception:
            available = False
        if not available:
            raise SecretStoreUnavailable('Secure credential storage is unavailable.')

    def _ref(self, conn, ref_id):
        row = conn.execute('SELECT * FROM secret_refs WHERE id=?', (ref_id,)).fetchone()
        if row is None:
            raise NotFound('Secret reference not found.')
        return SecretRef(**dict(row))

    def create(self, purpose, label, value):
        if purpose not in ('provider_api_key', 'github_app_private_key', 'github_token') or not isinstance(label, str) or not 1 <= len(label) <= 100 or not isinstance(value, str) or not value:
            raise ValueError('Invalid secret input.')
        with self.store.read():
            self._available()
            ref_id = new_id('sec')
            try:
                with self.store.transaction() as conn:
                    conn.execute('INSERT INTO secret_refs VALUES (?,?,?,?,?,NULL)', (ref_id, self.backend.name, purpose, label, self.clock()))
                    self.backend.put(ref_id, value.encode('utf-8'))
                    ref = self._ref(conn, ref_id)
            except Exception:
                # put may have partially succeeded, or the later COMMIT may have failed.
                try:
                    self.backend.delete(ref_id)
                except Exception:
                    pass
            else:
                return ref
            # Raise outside the except suite: the backend's secret-bearing exception
            # must not be retained in __context__ or __cause__.
            raise SecretStoreUnavailable('Unable to store credential securely.')

    def describe(self, ref_id):
        with self.store.read() as conn:
            return self._ref(conn, ref_id)

    def resolve(self, ref_id):
        with self.store.read() as conn:
            ref = self._ref(conn, ref_id)
            if ref.revoked_at is not None:
                raise SecretRevoked('Credential was revoked.')
            self._available()
            if ref.backend != self.backend.name:
                raise SecretMissing('Credential must be reconnected.')
            missing = False
            try:
                value = self.backend.get(ref_id).decode('utf-8')
            except KeyError:
                missing = True
            except Exception:
                pass
            else:
                return SecretValue(value)
            if missing:
                raise SecretMissing('Credential must be reconnected.')
            raise SecretStoreUnavailable('Unable to read credential securely.')

    def revoke(self, ref_id):
        with self.store.read():
            with self.store.transaction() as conn:
                ref = self._ref(conn, ref_id)
                if ref.revoked_at is None:
                    conn.execute('UPDATE secret_refs SET revoked_at=? WHERE id=?', (self.clock(), ref_id))
                ref = self._ref(conn, ref_id)
            try:
                self.backend.delete(ref_id)
            except Exception:
                pass
            return ref
