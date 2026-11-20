"""Persistent context schema; migration4 is append-only."""
INDEX_SQL = (
    """CREATE TABLE index_sources (
      id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('github','local','demo')),
      github_repository_id TEXT UNIQUE REFERENCES repositories(id), name TEXT NOT NULL,
      local_path TEXT, index_enabled INTEGER NOT NULL DEFAULT 1 CHECK(index_enabled IN (0,1)),
      exclusions_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}',
      active_snapshot_id TEXT REFERENCES repository_snapshots(id) DEFERRABLE INITIALLY DEFERRED,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
      CHECK((kind='github')=(github_repository_id IS NOT NULL)),
      CHECK((kind='local')=(local_path IS NOT NULL)))""",
    """CREATE TABLE embedding_namespaces (
      id TEXT PRIMARY KEY, provider_namespace_key TEXT NOT NULL,
      profile_version_id TEXT NOT NULL REFERENCES provider_profile_versions(id),
      dimensions INTEGER NOT NULL CHECK(dimensions>0), normalization TEXT NOT NULL,
      chunker_version TEXT NOT NULL, created_at INTEGER NOT NULL,
      UNIQUE(provider_namespace_key,dimensions,normalization,chunker_version))""",
    """CREATE TABLE repository_snapshots (
      id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES index_sources(id) ON DELETE CASCADE,
      revision TEXT NOT NULL, revision_kind TEXT NOT NULL CHECK(revision_kind IN ('git','content_hash')),
      branch TEXT, state TEXT NOT NULL CHECK(state IN ('staging','ready','retained','failed')),
      namespace_id TEXT REFERENCES embedding_namespaces(id), chunker_version TEXT NOT NULL,
      parser_version TEXT NOT NULL, manifest_hash TEXT, coverage_json TEXT NOT NULL DEFAULT '{}',
      profile_text TEXT NOT NULL DEFAULT '', profile_evidence_json TEXT NOT NULL DEFAULT '[]',
      created_at INTEGER NOT NULL, activated_at INTEGER)""",
    """CREATE TABLE snapshot_files (
      snapshot_id TEXT NOT NULL REFERENCES repository_snapshots(id) ON DELETE CASCADE,
      path TEXT NOT NULL, source_hash TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK(byte_count>=0),
      line_count INTEGER NOT NULL CHECK(line_count>=0), text TEXT NOT NULL,
      PRIMARY KEY(snapshot_id,path))""",
    """CREATE TABLE code_chunks (
      id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, path TEXT NOT NULL,
      start_line INTEGER NOT NULL CHECK(start_line>=1), end_line INTEGER NOT NULL CHECK(end_line>=start_line),
      content_hash TEXT NOT NULL, text TEXT NOT NULL, language TEXT, symbol TEXT,
      FOREIGN KEY(snapshot_id,path) REFERENCES snapshot_files(snapshot_id,path) ON DELETE CASCADE,
      UNIQUE(snapshot_id,path,start_line,end_line))""",
    "CREATE VIRTUAL TABLE code_chunks_fts USING fts5(chunk_id UNINDEXED,path_terms,text)",
    """CREATE TRIGGER code_chunks_delete_fts AFTER DELETE ON code_chunks BEGIN
      DELETE FROM code_chunks_fts WHERE chunk_id=old.id; END""",
    """CREATE TABLE repository_summaries (
      snapshot_id TEXT PRIMARY KEY REFERENCES repository_snapshots(id) ON DELETE CASCADE,
      text TEXT NOT NULL, evidence_json TEXT NOT NULL, profile_version_id TEXT REFERENCES provider_profile_versions(id),
      model TEXT, generation_json TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL)""",
    """CREATE TABLE index_runs (
      id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES index_sources(id) ON DELETE CASCADE,
      snapshot_id TEXT REFERENCES repository_snapshots(id) ON DELETE SET NULL,
      job_id TEXT REFERENCES jobs(id), state TEXT NOT NULL
      CHECK(state IN ('queued','running','waiting','paused','completed','failed','cancelled')),
      phase TEXT NOT NULL DEFAULT 'queued', continuation INTEGER NOT NULL DEFAULT 0,
      requested_revision TEXT, profile_version_id TEXT REFERENCES provider_profile_versions(id),
      limits_json TEXT NOT NULL DEFAULT '{}', checkpoint_json TEXT NOT NULL DEFAULT '{}',
      progress_json TEXT NOT NULL DEFAULT '{}', error_code TEXT, retry_at INTEGER,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
    "CREATE UNIQUE INDEX index_run_active ON index_runs(repository_id) WHERE state IN ('queued','running','waiting','paused')",
    "CREATE INDEX chunks_snapshot_path ON code_chunks(snapshot_id,path,start_line)",
    "CREATE INDEX snapshots_repository ON repository_snapshots(repository_id,created_at)",
    "CREATE INDEX index_run_state ON index_runs(state,updated_at)",
)

# Migration4 remains immutable; migration5 corrects namespace version identity.
INDEX_NAMESPACE_V5_SQL = (
    'PRAGMA defer_foreign_keys=ON',
    'CREATE TEMP TABLE migration5_namespace_backup AS SELECT * FROM embedding_namespaces',
    'DROP TABLE embedding_namespaces',
    """CREATE TABLE embedding_namespaces (
      id TEXT PRIMARY KEY, provider_namespace_key TEXT NOT NULL,
      profile_version_id TEXT NOT NULL REFERENCES provider_profile_versions(id),
      dimensions INTEGER NOT NULL CHECK(dimensions>0), normalization TEXT NOT NULL,
      chunker_version TEXT NOT NULL, created_at INTEGER NOT NULL,
      UNIQUE(provider_namespace_key,profile_version_id,dimensions,normalization,chunker_version))""",
    'INSERT INTO embedding_namespaces SELECT * FROM migration5_namespace_backup',
    'DROP TABLE migration5_namespace_backup',
)
