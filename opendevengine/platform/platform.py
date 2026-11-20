from dataclasses import dataclass
from .clock import now_ms
from .db import Store
from .jobs import JobQueue
from .secrets import SecretService, UnavailableSecretStore

@dataclass
class Platform:
    store: Store
    jobs: JobQueue
    secrets: SecretService

    @classmethod
    def open(cls, data_dir, *, secret_backend=None, clock=now_ms):
        store = Store.open(data_dir, clock=clock)
        return cls(store, JobQueue(store, clock=clock), SecretService(store, secret_backend if secret_backend is not None else UnavailableSecretStore(), clock=clock))

    def close(self):
        self.store.close()
