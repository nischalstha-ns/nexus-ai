-- 003: workflows (recorded demonstrations) + workflow runs (replay history)
CREATE TABLE IF NOT EXISTS workflows(
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  steps_json      TEXT NOT NULL DEFAULT '[]',
  created_from_task TEXT NULL REFERENCES tasks(id) ON DELETE SET NULL,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS workflow_runs(
  id              TEXT PRIMARY KEY,
  workflow_id     TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  task_id         TEXT NULL REFERENCES tasks(id) ON DELETE SET NULL,
  status          TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','awaiting_approval','done','failed','cancelled','reverted')),
  dry_run         INTEGER NOT NULL DEFAULT 0,
  checkpoint_dir  TEXT NULL,
  result_json     TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
