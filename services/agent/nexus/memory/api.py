"""Memory service: conversations, messages, long-term memories.

Privacy rules enforced here, not in callers:
- Memories default to consent='explicit'. Anything inferred by the agent is
  stored as 'inferred-pending' and is EXCLUDED from retrieval until the user
  approves it.
- export_user_data() / delete_user_data() give the user full control.
- Retrieval is relevance-based (FTS5 rank), never a full dump.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from ..core.errors import MemoryError
from ..core.logging import get_logger
from ..core.types import Message, Role
from .embeddings import EmbeddingProvider, cosine_similarity
from .store import Store

log = get_logger("memory.api")

KINDS = ("preference", "fact", "workflow", "episodic", "project")
CONSENTS = ("explicit", "inferred-pending", "system")


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class MemoryService:
    def __init__(self, store: Store):
        self.store = store

    # ---- conversations ----
    def create_conversation(self, title: str = "", project_id: str | None = None) -> str:
        cid = uuid.uuid4().hex
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO conversations(id, title, project_id) VALUES (?,?,?)",
                (cid, title, project_id))
        return cid

    def add_message(self, conversation_id: str, role: Role, content: str,
                    tool_name: str | None = None) -> int:
        with self.store.conn:
            cur = self.store.conn.execute(
                "INSERT INTO messages(conversation_id, role, content, tool_name)"
                " VALUES (?,?,?,?)",
                (conversation_id, role.value, content, tool_name))
            mid = cur.lastrowid
            self.store.conn.execute(
                "INSERT INTO messages_fts(rowid, content, conversation_id)"
                " VALUES (?,?,?)", (mid, content, conversation_id))
        return mid

    def get_messages(self, conversation_id: str, limit: int = 100) -> list[Message]:
        rows = self.store.conn.execute(
            "SELECT role, content, tool_name FROM messages"
            " WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit)).fetchall()
        return [Message(role=Role(r["role"]), content=r["content"],
                        tool_name=r["tool_name"]) for r in reversed(rows)]

    def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """SELECT m.id, m.conversation_id, m.role, snippet(messages_fts, 0, '…', '…', '…', 12) AS snip
               FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
               WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---- long-term memories ----
    def remember(self, kind: str, content: str, consent: str = "explicit",
                 project_id: str | None = None) -> str:
        if kind not in KINDS:
            raise MemoryError(f"unknown memory kind '{kind}'")
        if consent not in CONSENTS:
            raise MemoryError(f"unknown consent '{consent}'")
        mid = uuid.uuid4().hex
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO memories(id, kind, content, consent, project_id)"
                " VALUES (?,?,?,?,?)", (mid, kind, content, consent, project_id))
            rowid = self.store.conn.execute(
                "SELECT rowid FROM memories WHERE id=?", (mid,)).fetchone()["rowid"]
            self.store.conn.execute(
                "INSERT INTO memories_fts(rowid, content, kind) VALUES (?,?,?)",
                (rowid, content, kind))
        log.info("memory saved kind=%s consent=%s", kind, consent)
        return mid

    def approve(self, memory_id: str) -> None:
        """Promote an inferred-pending memory to explicit after user review."""
        with self.store.conn:
            cur = self.store.conn.execute(
                "UPDATE memories SET consent='explicit', updated_at=?"
                " WHERE id=? AND consent='inferred-pending'", (_utcnow(), memory_id))
            if cur.rowcount == 0:
                raise MemoryError(f"no pending memory '{memory_id}'")

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        """Single-memory fetch for the memory-browser UI (any consent state)."""
        row = self.store.conn.execute(
            "SELECT id, kind, content, consent, project_id, created_at, updated_at"
            " FROM memories WHERE id=?", (memory_id,)).fetchone()
        return dict(row) if row else None

    def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        """Review queue: inferred-pending memories awaiting user approval."""
        rows = self.store.conn.execute(
            "SELECT id, kind, content, created_at FROM memories"
            " WHERE consent='inferred-pending'"
            " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def update_memory(self, memory_id: str, content: str | None = None,
                      kind: str | None = None) -> dict[str, Any]:
        """Edit a memory's content and/or kind, keeping consent unchanged.

        The FTS index is rebuilt for the row so edited text is searchable.
        Pending memories stay pending (still need approve()).
        """
        if kind is not None and kind not in KINDS:
            raise MemoryError(f"unknown memory kind '{kind}'")
        if content is not None and not content.strip():
            raise MemoryError("content must not be empty")
        if content is None and kind is None:
            raise MemoryError("nothing to update")
        row = self.store.conn.execute(
            "SELECT rowid, content, kind FROM memories WHERE id=?",
            (memory_id,)).fetchone()
        if not row:
            raise MemoryError(f"unknown memory id '{memory_id}'")
        new_content = row["content"] if content is None else content
        new_kind = row["kind"] if kind is None else kind
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE memories SET content=?, kind=?, updated_at=?"
                " WHERE id=?", (new_content, new_kind, _utcnow(), memory_id))
            self.store.conn.execute(
                "DELETE FROM memories_fts WHERE rowid=?", (row["rowid"],))
            self.store.conn.execute(
                "INSERT INTO memories_fts(rowid, content, kind) VALUES (?,?,?)",
                (row["rowid"], new_content, new_kind))
        log.info("memory updated id=%s", memory_id)
        return self.get_memory(memory_id)  # type: ignore[return-value]

    def recall(self, query: str, limit: int = 5, kind: str | None = None) -> list[dict[str, Any]]:
        """Relevance-ranked recall. Pending memories are never returned."""
        sql = """SELECT m.id, m.kind, m.content, m.created_at, m.project_id
                 FROM memories_fts JOIN memories m ON m.id = (
                   SELECT id FROM memories WHERE rowid = memories_fts.rowid)
                 WHERE memories_fts MATCH ? AND m.consent != 'inferred-pending'"""
        params: list[Any] = [query]
        if kind:
            sql += " AND m.kind = ?"
            params.append(kind)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.store.conn.execute(sql, params).fetchall()]

    def forget(self, memory_id: str) -> bool:
        with self.store.conn:
            row = self.store.conn.execute(
                "SELECT rowid FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                return False
            self.store.conn.execute("DELETE FROM memories_fts WHERE rowid=?", (row["rowid"],))
            self.store.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        log.info("memory forgotten id=%s", memory_id)
        return True

    # ---- memory editing & review ----
    def update_memory(self, memory_id: str, content: str | None = None,
                      kind: str | None = None) -> dict[str, Any]:
        """Edit a memory's content and/or kind. FTS index rebuilt; embedding
        invalidated (recomputed lazily on next ensure_embeddings).
        Consent is unchanged by edits. Returns the updated row."""
        if content is None and kind is None:
            raise MemoryError("nothing to update")
        if content is not None and not content.strip():
            raise MemoryError("content must not be blank")
        if kind is not None and kind not in KINDS:
            raise MemoryError(f"unknown memory kind '{kind}'")
        with self.store.conn:
            row = self.store.conn.execute(
                "SELECT rowid, kind FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise MemoryError(f"no memory '{memory_id}'")
            if content is not None:
                self.store.conn.execute(
                    "UPDATE memories SET content=?, updated_at=? WHERE id=?",
                    (content, _utcnow(), memory_id))
                self.store.conn.execute(
                    "DELETE FROM memories_fts WHERE rowid=?", (row["rowid"],))
                self.store.conn.execute(
                    "INSERT INTO memories_fts(rowid, content, kind) VALUES (?,?,?)",
                    (row["rowid"], content, kind or row["kind"]))
                self.store.conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
            if kind is not None:
                self.store.conn.execute(
                    "UPDATE memories SET kind=?, updated_at=? WHERE id=?",
                    (kind, _utcnow(), memory_id))
            updated = self.store.conn.execute(
                "SELECT id, kind, content, consent, project_id, created_at, updated_at"
                " FROM memories WHERE id=?", (memory_id,)).fetchone()
        log.info("memory updated id=%s", memory_id)
        return dict(updated)

    def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        """Inferred memories awaiting user review."""
        rows = self.store.conn.execute(
            "SELECT id, kind, content, created_at FROM memories"
            " WHERE consent='inferred-pending' ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- semantic retrieval ----
    def ensure_embeddings(self, embedder: EmbeddingProvider, model: str = "",
                          batch_size: int = 32) -> int:
        """Backfill embeddings for memories that lack them. Returns count added."""
        rows = self.store.conn.execute(
            """SELECT m.id, m.content FROM memories m
               LEFT JOIN memory_embeddings e ON e.memory_id = m.id
               WHERE e.memory_id IS NULL AND m.consent != 'inferred-pending'""").fetchall()
        added = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            vecs = embedder.embed([r["content"] for r in batch])
            with self.store.conn:
                for r, v in zip(batch, vecs):
                    self.store.conn.execute(
                        "INSERT OR REPLACE INTO memory_embeddings"
                        "(memory_id, embedding_json, model) VALUES (?,?,?)",
                        (r["id"], json.dumps(v), model or embedder.name))
                    added += 1
        log.info("embedded %d memories with %s", added, embedder.name)
        return added

    def recall_semantic(self, query: str, embedder: EmbeddingProvider,
                        limit: int = 5, min_score: float = 0.0) -> list[dict[str, Any]]:
        """Cosine-similarity recall over stored embeddings.

        Pending (unapproved) memories are excluded, same as keyword recall.
        Embeddings are backfilled lazily on every call.
        """
        self.ensure_embeddings(embedder)
        qvec = embedder.embed([query])[0]
        rows = self.store.conn.execute(
            """SELECT m.id, m.kind, m.content, m.created_at, e.embedding_json
               FROM memories m JOIN memory_embeddings e ON e.memory_id = m.id
               WHERE m.consent != 'inferred-pending'""").fetchall()
        scored = []
        for r in rows:
            vec = json.loads(r["embedding_json"])
            score = cosine_similarity(qvec, vec)
            # Strictly greater: a zero cosine similarity means the texts
            # share nothing — it must never match, whatever min_score is.
            if score > min_score:
                d = dict(r)
                d["score"] = round(score, 4)
                del d["embedding_json"]
                scored.append(d)
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:limit]

    def recall_hybrid(self, query: str, embedder: EmbeddingProvider | None = None,
                      limit: int = 5, kind: str | None = None,
                      project_id: str | None = None) -> list[dict[str, Any]]:
        """Keyword (BM25) + semantic fusion via reciprocal rank fusion.

        Without an embedder (or with no embeddings stored) it degrades
        gracefully to pure keyword recall — never an error.
        """
        # Reciprocal rank fusion over the two ranked lists.
        fused: dict[str, float] = {}
        docs: dict[str, dict[str, Any]] = {}

        def add(doc_id: str, doc: dict[str, Any], rank: int):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (60 + rank)
            docs[doc_id] = doc

        for rank, d in enumerate(self.recall(query, limit=limit * 2, kind=kind)):
            if project_id and d.get("project_id") != project_id:
                continue
            add(d["id"], d, rank)

        if embedder is not None:
            try:
                for rank, d in enumerate(
                        self.recall_semantic(query, embedder, limit=limit * 2)):
                    if kind and d.get("kind") != kind:
                        continue
                    if project_id:
                        prow = self.store.conn.execute(
                            "SELECT project_id FROM memories WHERE id=?",
                            (d["id"],)).fetchone()
                        if not prow or prow["project_id"] != project_id:
                            continue
                    add(d["id"], d, rank)
            except Exception as e:
                log.warning("semantic recall failed, using keyword only: %s", e)

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [docs[doc_id] for doc_id, _ in ranked]

    def export_user_data(self) -> dict[str, Any]:
        c = self.store.conn
        return {
            "conversations": [dict(r) for r in c.execute("SELECT * FROM conversations")],
            "messages": [dict(r) for r in c.execute(
                "SELECT id, conversation_id, role, content, created_at FROM messages")],
            "memories": [dict(r) for r in c.execute("SELECT * FROM memories")],
            "tasks": [dict(r) for r in c.execute("SELECT * FROM tasks")],
        }

    def delete_user_data(self) -> None:
        """Irreversible wipe of user content (keeps schema + policies)."""
        with self.store.conn:
            c = self.store.conn
            c.execute("DELETE FROM messages_fts")
            c.execute("DELETE FROM memories_fts")
            c.execute("DELETE FROM tool_executions")
            c.execute("DELETE FROM tasks")
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM memories")
            c.execute("DELETE FROM conversations")
            c.execute("DELETE FROM audit_events")
        log.warning("user data wiped")
