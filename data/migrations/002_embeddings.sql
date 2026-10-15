-- NEXUS AI schema migration 002: vector embeddings for semantic memory.
-- Embeddings are stored as JSON arrays; cosine similarity is computed in
-- Python (SQLite has no vector extension in stdlib builds). memory_id is the
-- PK so each memory has at most one embedding; cascades on memory delete.

CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id      TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  embedding_json TEXT NOT NULL,
  model          TEXT NOT NULL DEFAULT '',
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
