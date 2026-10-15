"""Tests: memory service — CRUD, consent gating, FTS, export/delete."""
import os
import shutil
import tempfile
import unittest

from nexus.core.errors import MemoryError
from nexus.core.types import Role
from nexus.memory.api import MemoryService
from nexus.memory.store import Store

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                          "data", "migrations")


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"), MIGRATIONS)
        self.mem = MemoryService(self.store)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp)

    def test_migrations_applied(self):
        self.assertIn(1, self.store.applied_versions())

    def test_conversation_roundtrip(self):
        cid = self.mem.create_conversation("test")
        self.mem.add_message(cid, Role.USER, "hello")
        self.mem.add_message(cid, Role.ASSISTANT, "hi there")
        msgs = self.mem.get_messages(cid)
        self.assertEqual([m.content for m in msgs], ["hello", "hi there"])

    def test_search_messages(self):
        cid = self.mem.create_conversation()
        self.mem.add_message(cid, Role.USER, "the quick brown fox")
        hits = self.mem.search_messages("quick")
        self.assertEqual(len(hits), 1)
        self.assertIn("quick", hits[0]["snip"])

    def test_remember_and_recall(self):
        self.mem.remember("preference", "user likes dark mode")
        hits = self.mem.recall("dark mode")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "preference")

    def test_pending_memories_excluded_until_approved(self):
        mid = self.mem.remember("fact", "inferred birthday", consent="inferred-pending")
        self.assertEqual(self.mem.recall("birthday"), [])
        self.mem.approve(mid)
        self.assertEqual(len(self.mem.recall("birthday")), 1)

    def test_forget(self):
        mid = self.mem.remember("fact", "temporary note")
        self.assertTrue(self.mem.forget(mid))
        self.assertEqual(self.mem.recall("temporary"), [])
        self.assertFalse(self.mem.forget("nope"))

    def test_bad_kind_rejected(self):
        with self.assertRaises(MemoryError):
            self.mem.remember("nope", "x")

    def test_delete_user_data(self):
        cid = self.mem.create_conversation()
        self.mem.add_message(cid, Role.USER, "x")
        self.mem.remember("fact", "y")
        self.mem.delete_user_data()
        self.assertEqual(self.mem.get_messages(cid), [])
        self.assertEqual(self.mem.recall("y"), [])
        # schema survives
        self.assertIn(1, self.store.applied_versions())

    # ---- Phase 4: editing + review queue ----
    def test_get_memory(self):
        mid = self.mem.remember("fact", "editable note")
        got = self.mem.get_memory(mid)
        self.assertIsNotNone(got)
        self.assertEqual(got["content"], "editable note")
        self.assertEqual(got["kind"], "fact")
        self.assertIsNone(self.mem.get_memory("nope"))

    def test_update_memory_content(self):
        mid = self.mem.remember("fact", "old wording here")
        got = self.mem.update_memory(mid, content="new wording here")
        self.assertEqual(got["content"], "new wording here")
        self.assertEqual(len(self.mem.recall("new wording")), 1)
        self.assertEqual(self.mem.recall("old wording"), [])

    def test_update_memory_kind(self):
        mid = self.mem.remember("fact", "a note")
        got = self.mem.update_memory(mid, kind="preference")
        self.assertEqual(got["kind"], "preference")
        self.assertEqual(len(self.mem.recall("a note", kind="preference")), 1)

    def test_update_memory_rejected(self):
        mid = self.mem.remember("fact", "a note")
        with self.assertRaises(MemoryError):
            self.mem.update_memory(mid, kind="nope")
        with self.assertRaises(MemoryError):
            self.mem.update_memory(mid, content="   ")
        with self.assertRaises(MemoryError):
            self.mem.update_memory(mid)
        with self.assertRaises(MemoryError):
            self.mem.update_memory("nope", content="x")

    def test_update_keeps_consent_pending(self):
        mid = self.mem.remember("fact", "inferred note", consent="inferred-pending")
        self.mem.update_memory(mid, content="corrected inferred note")
        self.assertEqual(self.mem.recall("corrected inferred"), [])
        self.mem.approve(mid)
        self.assertEqual(len(self.mem.recall("corrected inferred")), 1)

    def test_list_pending(self):
        self.mem.remember("fact", "explicit note")
        pid = self.mem.remember("fact", "pending note", consent="inferred-pending")
        pending = self.mem.list_pending()
        self.assertEqual([p["id"] for p in pending], [pid])
        self.mem.approve(pid)
        self.assertEqual(self.mem.list_pending(), [])

    def test_approve_unknown_raises(self):
        with self.assertRaises(MemoryError):
            self.mem.approve("nope")


if __name__ == "__main__":
    unittest.main()


class SemanticMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"), MIGRATIONS)
        self.mem = MemoryService(self.store)
        from nexus.memory.embeddings import MockEmbeddings, cosine_similarity
        self.embedder = MockEmbeddings()
        self.cosine_similarity = cosine_similarity

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp)

    def test_migration_002_applied(self):
        self.assertIn(2, self.store.applied_versions())

    def test_cosine_similarity(self):
        self.assertAlmostEqual(self.cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(self.cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_mock_embeddings_deterministic(self):
        a = self.embedder.embed(["hello world"])[0]
        b = self.embedder.embed(["hello world"])[0]
        self.assertEqual(a, b)
        self.assertEqual(len(a), 128)

    def test_ensure_embeddings_backfills(self):
        self.mem.remember("fact", "the cat sat on the mat")
        self.mem.remember("fact", "quantum physics lecture notes")
        n = self.mem.ensure_embeddings(self.embedder)
        self.assertEqual(n, 2)
        # second run: nothing new
        self.assertEqual(self.mem.ensure_embeddings(self.embedder), 0)

    def test_semantic_recall_backfills_lazily(self):
        # No explicit ensure_embeddings call: recall must backfill itself.
        self.mem.remember("fact", "the cat sat on the mat")
        hits = self.mem.recall_semantic("feline on a rug", self.embedder)
        self.assertTrue(any("cat" in h["content"] for h in hits))

    def test_zero_similarity_never_matches(self):
        self.mem.remember("fact", "quantum physics lecture notes")
        # 'zebra' shares no tokens -> similarity exactly 0 -> excluded
        # even with the default min_score of 0.0.
        hits = self.mem.recall_semantic("zebra", self.embedder, min_score=0.0)
        self.assertEqual(hits, [])

    def test_semantic_recall_ranks_related(self):
        self.mem.remember("fact", "the cat sat on the mat")
        self.mem.remember("fact", "quantum physics lecture notes")
        self.mem.ensure_embeddings(self.embedder)
        hits = self.mem.recall_semantic("feline sitting on rug", self.embedder)
        self.assertEqual(len(hits), 2)
        # cat-related memory should outrank physics (token overlap geometry)
        self.assertIn("cat", hits[0]["content"])

    def test_semantic_excludes_pending(self):
        self.mem.remember("fact", "pending secret note", consent="inferred-pending")
        self.mem.ensure_embeddings(self.embedder)
        hits = self.mem.recall_semantic("secret note", self.embedder)
        self.assertEqual(hits, [])

    def test_hybrid_without_embedder_falls_back(self):
        self.mem.remember("fact", "hybrid fallback note")
        hits = self.mem.recall_hybrid("hybrid fallback", None)
        self.assertEqual(len(hits), 1)

    def test_hybrid_combines(self):
        self.mem.remember("fact", "the cat sat on the mat")
        self.mem.remember("fact", "quantum physics lecture notes")
        self.mem.ensure_embeddings(self.embedder)
        hits = self.mem.recall_hybrid("feline mat", self.embedder)
        self.assertTrue(any("cat" in h["content"] for h in hits))

    def test_update_invalidates_embedding(self):
        mid = self.mem.remember("fact", "original wording here")
        self.mem.ensure_embeddings(self.embedder)
        self.mem.update_memory(mid, content="completely different wording now")
        rows = self.store.conn.execute(
            "SELECT COUNT(*) c FROM memory_embeddings WHERE memory_id=?",
            (mid,)).fetchone()
        self.assertEqual(rows["c"], 0)
