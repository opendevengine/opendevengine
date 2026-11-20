"""Persistent repository context.

Injected snapshot_reader(source, revision, limits) -> mapping with revision,
revision_kind ('git' or 'content_hash'), optional branch, and a lazy `files`
iterable. Each file is {path, content: bytes} or {path, kind: skip_reason}.
The reader must resolve immutable revisions, bound acquisition using the supplied
limits, and perform no code execution. The service revalidates all input. On
resume it receives the already pinned revision and may replay entries; persisted
files/chunks and vectors are idempotent. Internal source._ingested_paths is a
set of already retained paths: readers may yield {path, kind: "resumed"} for those
entries instead of refetching their bodies; replay must preserve entry order. Source contains private local_path and
exclusions and must never be logged or exposed through HTTP.
"""
from .service import IndexService, IndexError
from .vectors import VectorStore, VectorUnavailable

__all__ = ['IndexService', 'IndexError', 'VectorStore', 'VectorUnavailable']
