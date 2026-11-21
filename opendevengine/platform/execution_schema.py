"""Immutable execution profiles and preflight records."""
EXECUTION_SQL = (
    """CREATE TABLE execution_profiles (
      id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE COLLATE NOCASE,
      active_version_id TEXT NOT NULL REFERENCES execution_profile_versions(id) DEFERRABLE INITIALLY DEFERRED,
      deleted_at INTEGER,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)""",
    """CREATE TABLE execution_profile_versions (
      id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES execution_profiles(id),
      version INTEGER NOT NULL CHECK(version>=1),adapter TEXT NOT NULL CHECK(adapter='claude_code'),
      auth_mode TEXT NOT NULL CHECK(auth_mode='cli_login'),config_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,UNIQUE(profile_id,version))""",
    """CREATE TABLE execution_profile_checks (
      version_id TEXT PRIMARY KEY REFERENCES execution_profile_versions(id),
      job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
      status TEXT NOT NULL CHECK(status IN ('queued','running','passed','failed','interrupted')),
      result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT,checked_at INTEGER)""",
    """CREATE TABLE execution_settings (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),max_active_sessions INTEGER NOT NULL DEFAULT 1
      CHECK(max_active_sessions BETWEEN 1 AND 4),updated_at INTEGER NOT NULL)""",
    """CREATE TABLE runtime_instance (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),id TEXT NOT NULL UNIQUE,created_at INTEGER NOT NULL)""",
)

# v16: Claude Code may run with an Anthropic API key held in the vault; the AI providers page
# owns one managed execution profile and records which profiles it created.
EXECUTION_V16_SQL = (
    """CREATE TABLE execution_profile_versions_v16 (
      id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES execution_profiles(id),
      version INTEGER NOT NULL CHECK(version>=1),adapter TEXT NOT NULL CHECK(adapter='claude_code'),
      auth_mode TEXT NOT NULL CHECK(auth_mode IN ('cli_login','api_key')),config_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,secret_ref TEXT REFERENCES secret_refs(id),
      CHECK((auth_mode='api_key')=(secret_ref IS NOT NULL)),UNIQUE(profile_id,version))""",
    """INSERT INTO execution_profile_versions_v16 (id,profile_id,version,adapter,auth_mode,config_json,created_at)
      SELECT id,profile_id,version,adapter,auth_mode,config_json,created_at FROM execution_profile_versions""",
    "DROP TABLE execution_profile_versions",
    "ALTER TABLE execution_profile_versions_v16 RENAME TO execution_profile_versions",
    "ALTER TABLE execution_profiles ADD COLUMN managed INTEGER NOT NULL DEFAULT 0 CHECK(managed IN (0,1))",
    """CREATE TABLE ai_providers (
      id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('claude','codex','local')),
      name TEXT NOT NULL,endpoint TEXT,auth TEXT NOT NULL CHECK(auth IN ('api_key','cli_login','none')),
      secret_ref TEXT REFERENCES secret_refs(id),created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)""",
    """CREATE TABLE ai_provider_profiles (
      provider_id TEXT NOT NULL REFERENCES ai_providers(id),
      kind TEXT NOT NULL CHECK(kind IN ('provider','execution')),profile_id TEXT NOT NULL,
      model TEXT NOT NULL,PRIMARY KEY(kind,profile_id))""",
    """CREATE TABLE ai_job_choices (
      job TEXT PRIMARY KEY CHECK(job IN ('plans','coding','search','summary')),
      provider_id TEXT NOT NULL REFERENCES ai_providers(id),model TEXT NOT NULL,updated_at INTEGER NOT NULL)""",
)
