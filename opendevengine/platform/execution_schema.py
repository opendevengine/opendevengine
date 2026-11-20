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
