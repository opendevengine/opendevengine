"""Durable draft-publication operations and reconciliation state."""

PUBLICATION_SQL = (
    """CREATE TABLE publication_settings (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
      commit_name TEXT CHECK(commit_name IS NULL OR length(commit_name) BETWEEN 1 AND 100),
      commit_email TEXT CHECK(commit_email IS NULL OR length(commit_email) BETWEEN 3 AND 254),
      updated_at INTEGER NOT NULL)""",
    """CREATE TABLE publications (
      id TEXT PRIMARY KEY,session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
      repository_id TEXT NOT NULL,job_id TEXT REFERENCES jobs(id),
      idempotency_key TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,
      intent_version INTEGER NOT NULL CHECK(intent_version=1),
      policy TEXT NOT NULL CHECK(policy IN ('manual_draft','on_success_draft')),
      expected_diff_hash TEXT NOT NULL,checkpoint_artifact_id TEXT REFERENCES session_artifacts(id) ON DELETE SET NULL,
      patch_artifact_id TEXT REFERENCES session_artifacts(id) ON DELETE SET NULL,
      expected_base_sha TEXT NOT NULL,base_branch TEXT NOT NULL,head_branch TEXT NOT NULL,
      title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 256),body TEXT NOT NULL CHECK(length(body)<=65536),
      commit_name TEXT NOT NULL CHECK(length(commit_name) BETWEEN 1 AND 100),
      commit_email TEXT NOT NULL CHECK(length(commit_email) BETWEEN 3 AND 254),
      commit_date TEXT NOT NULL,commit_message TEXT NOT NULL CHECK(length(commit_message) BETWEEN 1 AND 4096),
      state TEXT NOT NULL CHECK(state IN
       ('queued','preparing','creating_objects','creating_branch','creating_pr','reconciling',
        'published','blocked','failed','no_changes')),
      remote_base_sha TEXT,base_tree_sha TEXT,tree_sha TEXT,commit_sha TEXT,head_sha TEXT,
      branch_created INTEGER NOT NULL DEFAULT 0 CHECK(branch_created IN (0,1)),
      pull_request_id TEXT,pull_request_number INTEGER CHECK(pull_request_number IS NULL OR pull_request_number>0),
      pull_request_url TEXT,pull_request_state TEXT CHECK(pull_request_state IS NULL OR pull_request_state IN ('open','closed','merged')),
      error_code TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,finished_at INTEGER)""",
    "CREATE INDEX publication_state ON publications(state,updated_at)",
    "CREATE INDEX publication_repository ON publications(repository_id,created_at)",
    """CREATE TABLE publication_events (
      publication_id TEXT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      seq INTEGER NOT NULL CHECK(seq>=1),type TEXT NOT NULL,payload_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,PRIMARY KEY(publication_id,seq))""",
)
