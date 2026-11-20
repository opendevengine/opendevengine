"""Background reconciliation markers, independent of source text."""
SCHEDULING_SQL = (
    """CREATE TABLE index_schedule_state (
      repository_id TEXT PRIMARY KEY REFERENCES index_sources(id) ON DELETE CASCADE,
      signature TEXT NOT NULL, run_id TEXT REFERENCES index_runs(id) ON DELETE SET NULL,
      requested_at INTEGER NOT NULL)""",
)
