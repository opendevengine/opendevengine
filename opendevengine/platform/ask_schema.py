"""Durable routing, conversation and immutable plan storage."""
ASK_SQL = (
    """CREATE TABLE conversations (
      id TEXT PRIMARY KEY, title TEXT NOT NULL, scope_json TEXT NOT NULL DEFAULT '[]',
      scope_revision INTEGER NOT NULL DEFAULT 1 CHECK(scope_revision>=1),
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER)""",
    """CREATE TABLE conversation_messages (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
      role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT,
      state TEXT NOT NULL CHECK(state IN ('pending','complete','failed','cancelled','redacted')),
      client_request_id TEXT, citations_json TEXT NOT NULL DEFAULT '[]',
      profile_version_id TEXT REFERENCES provider_profile_versions(id),
      usage_json TEXT NOT NULL DEFAULT '{}', error_code TEXT, created_at INTEGER NOT NULL,
      UNIQUE(conversation_id,client_request_id,role))""",
    """CREATE TABLE routing_decisions (
      id TEXT PRIMARY KEY, conversation_id TEXT REFERENCES conversations(id), prompt TEXT NOT NULL,
      pinned_json TEXT NOT NULL DEFAULT '[]', selected_json TEXT NOT NULL DEFAULT '[]',
      candidates_json TEXT NOT NULL DEFAULT '[]', catalog_generation TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('resolved','needs_selection','no_match','degraded')),
      degradation_code TEXT, created_at INTEGER NOT NULL)""",
    """CREATE TABLE answer_runs (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
      user_message_id TEXT NOT NULL REFERENCES conversation_messages(id),
      assistant_message_id TEXT NOT NULL REFERENCES conversation_messages(id),
      job_id TEXT REFERENCES jobs(id), scope_revision INTEGER NOT NULL, scope_json TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','cancelled')),
      error_code TEXT, routing_decision_id TEXT REFERENCES routing_decisions(id),
      generation_metadata_json TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
      UNIQUE(user_message_id))""",
    "CREATE UNIQUE INDEX answer_active ON answer_runs(conversation_id) WHERE state IN ('queued','running')",
    """CREATE TABLE plans (
      id TEXT PRIMARY KEY, conversation_id TEXT REFERENCES conversations(id), title TEXT NOT NULL,
      latest_version_id TEXT NOT NULL REFERENCES plan_versions(id) DEFERRABLE INITIALLY DEFERRED,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER)""",
    """CREATE TABLE plan_versions (
      id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id),
      version INTEGER NOT NULL CHECK(version>=1), parent_version_id TEXT REFERENCES plan_versions(id),
      scope_revision INTEGER CHECK(scope_revision IS NULL OR scope_revision>=1),
      content_json TEXT, content_hash TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('manual','model','amendment')),
      profile_version_id TEXT REFERENCES provider_profile_versions(id), generation_json TEXT NOT NULL DEFAULT '{}',
      created_at INTEGER NOT NULL, deleted_at INTEGER, UNIQUE(plan_id,version),
      CHECK(content_json IS NOT NULL OR deleted_at IS NOT NULL))""",
    "CREATE INDEX messages_conversation ON conversation_messages(conversation_id,created_at,id)",
    "CREATE INDEX routing_conversation ON routing_decisions(conversation_id,created_at,id)",
    "CREATE INDEX plans_conversation ON plans(conversation_id,created_at,id)",
    "CREATE INDEX answer_state ON answer_runs(state,updated_at)",
)
