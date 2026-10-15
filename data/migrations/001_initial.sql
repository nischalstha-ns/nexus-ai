-- NEXUS AI schema migration 001: initial schema
-- Covers: conversations, messages, memories (+FTS), projects, tasks/plans,
-- tool executions, permission policies, providers, models, settings, audit.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version   INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS conversations (
  id         TEXT PRIMARY KEY,
  title      TEXT NOT NULL DEFAULT '',
  project_id TEXT NULL REFERENCES projects(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
  content         TEXT NOT NULL DEFAULT '',
  tool_name       TEXT NULL,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

-- Full-text search over messages (content only; metadata stays relational).
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content, conversation_id UNINDEXED, tokenize='porter'
);

CREATE TABLE IF NOT EXISTS projects (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  root_path  TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Long-term memory. `consent` records HOW this fact was approved:
-- 'explicit' (user said "remember this"), 'inferred-pending' (needs review),
-- 'system' (non-personal operational fact).
CREATE TABLE IF NOT EXISTS memories (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('preference','fact','workflow','episodic','project')),
  content    TEXT NOT NULL,
  consent    TEXT NOT NULL DEFAULT 'explicit' CHECK (consent IN ('explicit','inferred-pending','system')),
  project_id TEXT NULL REFERENCES projects(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content, kind UNINDEXED, tokenize='porter'
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);

CREATE TABLE IF NOT EXISTS tasks (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NULL REFERENCES conversations(id) ON DELETE SET NULL,
  title           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','running','awaiting_approval','awaiting_input','done','failed','cancelled')),
  plan_json       TEXT NOT NULL DEFAULT '[]',
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS tool_executions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      TEXT NULL REFERENCES tasks(id) ON DELETE SET NULL,
  tool_name    TEXT NOT NULL,
  args_json    TEXT NOT NULL DEFAULT '{}',
  permission   TEXT NOT NULL,
  approved_by  TEXT NULL,
  status       TEXT NOT NULL CHECK (status IN ('ok','error','denied','timeout','cancelled')),
  result_json  TEXT NOT NULL DEFAULT '{}',
  duration_ms  INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_tool_exec_tool ON tool_executions(tool_name);

CREATE TABLE IF NOT EXISTS permission_policies (
  tool_name  TEXT PRIMARY KEY,
  level      TEXT NOT NULL CHECK (level IN ('observe','assist','execute','elevated')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS providers (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('openai_compatible','ollama','mock')),
  base_url   TEXT NOT NULL,
  model_id   TEXT NOT NULL,
  is_local   INTEGER NOT NULL DEFAULT 0,
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS user_settings (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL DEFAULT '""',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Append-only audit log. Never updated or deleted by the app.
CREATE TABLE IF NOT EXISTS audit_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  category   TEXT NOT NULL,
  summary    TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_cat ON audit_events(category, id);
