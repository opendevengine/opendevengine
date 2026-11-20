"""Durable implementation sessions and bounded artifacts."""
SESSION_SQL = (
    """CREATE TABLE sessions (
      id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES plans(id),
      plan_version_id TEXT NOT NULL REFERENCES plan_versions(id),content_hash TEXT NOT NULL,
      profile_version_id TEXT NOT NULL REFERENCES execution_profile_versions(id),
      idempotency_key TEXT NOT NULL UNIQUE,launch_hash TEXT NOT NULL,
      predecessor_id TEXT REFERENCES sessions(id),attempt INTEGER NOT NULL CHECK(attempt>=1),
      job_id TEXT REFERENCES jobs(id),state TEXT NOT NULL CHECK(state IN
       ('queued','provisioning','running','validating','waiting_for_user','stopping','completed','blocked','failed','cancelled','interrupted')),
      phase TEXT NOT NULL DEFAULT 'queued',stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0,1)),
      stop_reason TEXT,error_code TEXT,profile_snapshot_json TEXT NOT NULL DEFAULT '{}',
      limits_json TEXT NOT NULL DEFAULT '{}',instructions_json TEXT NOT NULL DEFAULT '{}',
      usage_json TEXT NOT NULL DEFAULT '{}',publication_json TEXT NOT NULL DEFAULT '{}',
      outcome_json TEXT NOT NULL DEFAULT '{}',created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
      started_at INTEGER,finished_at INTEGER)""",
    "CREATE INDEX session_state ON sessions(state,updated_at)",
    """CREATE TABLE session_repositories (
      session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      repository_id TEXT NOT NULL REFERENCES index_sources(id),role TEXT NOT NULL CHECK(role IN ('write','read')),
      snapshot_id TEXT REFERENCES repository_snapshots(id) ON DELETE SET NULL,
      base_sha TEXT NOT NULL,base_branch TEXT,session_branch TEXT,
      base_manifest_json TEXT NOT NULL DEFAULT '{}',source_manifest_json TEXT NOT NULL DEFAULT '{}',
      current_diff_hash TEXT,PRIMARY KEY(session_id,repository_id))""",
    """CREATE TABLE session_runners (
      session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
      instance_id TEXT NOT NULL,container_id TEXT,harness_pid INTEGER,harness_start TEXT,
      state TEXT NOT NULL CHECK(state IN ('provisioning','running','terminating','terminated','containment_failed')),
      private_json TEXT NOT NULL DEFAULT '{}',updated_at INTEGER NOT NULL)""",
    """CREATE TABLE session_events (
      session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,seq INTEGER NOT NULL CHECK(seq>=1),
      type TEXT NOT NULL,payload_json TEXT NOT NULL,created_at INTEGER NOT NULL,
      PRIMARY KEY(session_id,seq))""",
    """CREATE TABLE session_messages (
      id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      client_request_id TEXT NOT NULL,content TEXT,question_id TEXT REFERENCES session_questions(id) ON DELETE SET NULL,
      delivery TEXT NOT NULL CHECK(delivery IN ('queued','delivered','acknowledged','rejected')),
      reason TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,UNIQUE(session_id,client_request_id))""",
    """CREATE TABLE session_questions (
      id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      prompt TEXT,answer_message_id TEXT REFERENCES session_messages(id) ON DELETE SET NULL,
      state TEXT NOT NULL CHECK(state IN ('open','answered','cancelled')),created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)""",
    "CREATE UNIQUE INDEX session_one_question ON session_questions(session_id) WHERE state='open'",
    """CREATE TABLE session_artifacts (
      id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK(kind IN ('checkpoint','patch','validation_log','manifest','context')),
      sha256 TEXT NOT NULL,byte_count INTEGER NOT NULL CHECK(byte_count>=0),content BLOB,
      created_at INTEGER NOT NULL,CHECK(content IS NULL OR length(content)=byte_count))""",
    """CREATE TABLE validation_runs (
      id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      ordinal INTEGER NOT NULL,command_json TEXT NOT NULL,source TEXT NOT NULL CHECK(source IN ('observed','proposed')),
      status TEXT NOT NULL CHECK(status IN ('passed','failed','timeout','error','not_run','unavailable')),
      required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),exit_code INTEGER,
      artifact_id TEXT REFERENCES session_artifacts(id) ON DELETE SET NULL,environment_json TEXT NOT NULL DEFAULT '{}',
      started_at INTEGER,finished_at INTEGER,UNIQUE(session_id,ordinal))""",
    "CREATE INDEX messages_session ON session_messages(session_id,created_at,id)",
    "CREATE INDEX artifacts_session ON session_artifacts(session_id,created_at,id)",
)
