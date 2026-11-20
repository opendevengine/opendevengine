"""Service-layer migrations. Existing migration1 remains immutable."""
GITHUB_SQL = (
    """CREATE TABLE github_connections (
      id TEXT PRIMARY KEY, app_id TEXT NOT NULL UNIQUE, slug TEXT NOT NULL,
      private_key_ref TEXT NOT NULL REFERENCES secret_refs(id),
      status TEXT NOT NULL CHECK(status IN ('configured','connecting','connected','pending_approval','reconnect_required','disconnected')),
      index_new INTEGER NOT NULL DEFAULT 0 CHECK(index_new IN (0,1)),
      refresh_interval_ms INTEGER NOT NULL DEFAULT 3600000 CHECK(refresh_interval_ms>=900000),
      last_error_code TEXT, last_refresh_at INTEGER, next_retry_at INTEGER,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
    """CREATE TABLE github_setup_attempts (
      state_hash TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES github_connections(id),
      expires_at INTEGER NOT NULL, consumed_at INTEGER)""",
    """CREATE TABLE github_installations (
      id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES github_connections(id),
      owner TEXT NOT NULL, account_type TEXT NOT NULL, permissions TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL CHECK(status IN ('active','suspended','revoked')),
      repository_selection TEXT NOT NULL DEFAULT 'selected', suspended_at TEXT, last_refresh_at INTEGER)""",
    """CREATE TABLE repositories (
      id TEXT PRIMARY KEY, github_id INTEGER NOT NULL UNIQUE, owner TEXT NOT NULL, name TEXT NOT NULL,
      full_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', topics TEXT NOT NULL DEFAULT '[]',
      default_branch TEXT NOT NULL, language TEXT, visibility TEXT NOT NULL,
      archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
      fork INTEGER NOT NULL DEFAULT 0 CHECK(fork IN (0,1)), clone_url TEXT NOT NULL, pushed_at TEXT,
      index_enabled INTEGER NOT NULL DEFAULT 0 CHECK(index_enabled IN (0,1)),
      implementation_enabled INTEGER NOT NULL DEFAULT 0 CHECK(implementation_enabled IN (0,1)),
      access_state TEXT NOT NULL DEFAULT 'unknown' CHECK(access_state IN ('accessible','inaccessible','unknown')),
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
    """CREATE TABLE repository_installations (
      repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
      installation_id TEXT NOT NULL REFERENCES github_installations(id),
      permissions TEXT NOT NULL DEFAULT '{}', seen_generation TEXT NOT NULL,
      PRIMARY KEY(repository_id,installation_id))""",
    """CREATE TABLE discovery_runs (
      id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES github_connections(id),
      generation TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','running','waiting','completed','failed','cancelled')),
      job_id TEXT REFERENCES jobs(id), continuation INTEGER NOT NULL DEFAULT 0,
      checkpoint TEXT NOT NULL DEFAULT '{}', discovered_count INTEGER NOT NULL DEFAULT 0,
      error_code TEXT, retry_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
    "CREATE UNIQUE INDEX discovery_active ON discovery_runs(connection_id) WHERE state IN ('queued','running','waiting')",
    "CREATE INDEX repository_catalog ON repositories(access_state,owner,id)",
    "CREATE INDEX installation_connection ON github_installations(connection_id,status)",
)

PROVIDER_SQL = (
    """CREATE TABLE provider_profiles (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, active_version_id TEXT NOT NULL
      REFERENCES provider_profile_versions(id) DEFERRABLE INITIALLY DEFERRED,
      deleted_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
    "CREATE UNIQUE INDEX provider_name_active ON provider_profiles(name) WHERE deleted_at IS NULL",
    """CREATE TABLE provider_profile_versions (
      id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES provider_profiles(id),
      version INTEGER NOT NULL CHECK(version>=1), adapter TEXT NOT NULL CHECK(adapter IN ('ollama','openai_compatible')),
      endpoint TEXT NOT NULL, model TEXT NOT NULL, secret_ref TEXT REFERENCES secret_refs(id),
      config TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(profile_id,version))""",
    """CREATE TABLE model_role_assignments (
      role TEXT PRIMARY KEY CHECK(role IN ('ask','planning','summary','embedding')),
      profile_id TEXT NOT NULL REFERENCES provider_profiles(id), updated_at INTEGER NOT NULL)""",
    """CREATE TABLE provider_settings (
      key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)""",
    """CREATE TABLE provider_version_checks (
      version_id TEXT PRIMARY KEY REFERENCES provider_profile_versions(id), status TEXT NOT NULL,
      verified_capabilities TEXT NOT NULL DEFAULT '[]', error_code TEXT, checked_at INTEGER NOT NULL)""",
    """CREATE TABLE provider_usage (
      id TEXT PRIMARY KEY, profile_version_id TEXT NOT NULL REFERENCES provider_profile_versions(id),
      purpose TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, estimated_cost REAL,
      cost_status TEXT NOT NULL CHECK(cost_status IN ('estimated','unknown')), subject_type TEXT,
      subject_id TEXT, outcome TEXT NOT NULL DEFAULT 'success', currency TEXT NOT NULL DEFAULT 'USD',
      created_at INTEGER NOT NULL)""",
)

# v12: add the claude_cli adapter. SQLite cannot alter a CHECK, so the versions table is
# rebuilt with identical columns and rows (Migration.rebuild_tables).
PROVIDER_ADAPTER_V12_SQL = (
    """CREATE TABLE provider_profile_versions_v12 (
      id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES provider_profiles(id),
      version INTEGER NOT NULL CHECK(version>=1),
      adapter TEXT NOT NULL CHECK(adapter IN ('ollama','openai_compatible','claude_cli')),
      endpoint TEXT NOT NULL, model TEXT NOT NULL, secret_ref TEXT REFERENCES secret_refs(id),
      config TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(profile_id,version))""",
    """INSERT INTO provider_profile_versions_v12
      (id, profile_id, version, adapter, endpoint, model, secret_ref, config, created_at)
      SELECT id, profile_id, version, adapter, endpoint, model, secret_ref, config, created_at
      FROM provider_profile_versions""",
    "DROP TABLE provider_profile_versions",
    "ALTER TABLE provider_profile_versions_v12 RENAME TO provider_profile_versions",
)
