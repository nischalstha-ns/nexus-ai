"""Tests: the HTTP service — endpoints, persistence, SSE, provider config."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request

from nexus.core.config import Settings
from nexus.service.app import serve


def req(method, url, body=None):
    import urllib.error
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def sse_events(url, body):
    data = json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    events = []
    with urllib.request.urlopen(r, timeout=15) as resp:
        buf = ""
        while True:
            chunk = resp.read(1024)
            if not chunk:
                break
            buf += chunk.decode()
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                for line in raw.splitlines():
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            return events
                        events.append(json.loads(payload))
    return events


class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["NEXUS_DATA_DIR"] = cls.tmp
        s = Settings()
        s.port = 0  # ephemeral
        cls.server = serve(s)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_health(self):
        code, body = req("GET", f"{self.base}/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(any(p["reachable"] for p in body["providers"]))

    def test_chat_turn_persists(self):
        code, body = req("POST", f"{self.base}/chat",
                         {"message": "hello nexus"})
        self.assertEqual(code, 200)
        cid = body["conversation_id"]
        self.assertEqual(body["status"], "done")
        code, msgs = req("GET", f"{self.base}/conversations/{cid}/messages")
        roles = [m["role"] for m in msgs["messages"]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_chat_stream_sse(self):
        events = sse_events(f"{self.base}/chat/stream", {"message": "hi"})
        kinds = [e["kind"] for e in events]
        self.assertIn("done", kinds)
        self.assertTrue(all("conversation_id" in e for e in events))

    def test_chat_requires_message(self):
        code, body = req("POST", f"{self.base}/chat", {})
        self.assertEqual(code, 400)

    def test_provider_crud(self):
        code, _ = req("POST", f"{self.base}/providers",
                      {"id": "local-llama", "kind": "ollama",
                       "base_url": "http://127.0.0.1:11434/v1",
                       "model_id": "llama3.1", "is_local": True})
        self.assertEqual(code, 200)
        code, body = req("GET", f"{self.base}/providers")
        self.assertTrue(any(p["id"] == "local-llama" for p in body["providers"]))
        # secrets must never appear
        self.assertNotIn("api_key", json.dumps(body))
        code, _ = req("DELETE", f"{self.base}/providers/local-llama")
        self.assertEqual(code, 200)

    def test_memory_endpoints(self):
        code, body = req("POST", f"{self.base}/memories",
                         {"kind": "preference", "content": "likes tea"})
        self.assertEqual(code, 200)
        code, found = req("GET", f"{self.base}/memories/search?q=tea")
        self.assertTrue(any("tea" in m["content"] for m in found["memories"]))
        code, _ = req("DELETE", f"{self.base}/memories/{body['id']}")
        self.assertEqual(code, 200)

    def test_conversation_crud(self):
        code, body = req("POST", f"{self.base}/conversations", {"title": "t"})
        cid = body["id"]
        code, body = req("GET", f"{self.base}/conversations")
        self.assertTrue(any(c["id"] == cid for c in body["conversations"]))
        code, _ = req("DELETE", f"{self.base}/conversations/{cid}")
        self.assertEqual(code, 200)

    def test_conversation_search(self):
        code, _ = req("POST", f"{self.base}/chat",
                      {"message": "the quick brown platypus"})
        self.assertEqual(code, 200)
        code, body = req("GET", f"{self.base}/conversations/search?q=platypus")
        self.assertEqual(code, 200)
        self.assertTrue(any("platypus" in h["snip"] for h in body["hits"]))
        code, _ = req("GET", f"{self.base}/conversations/search")
        self.assertEqual(code, 400)

    def test_memory_edit_flow(self):
        code, body = req("POST", f"{self.base}/memories",
                         {"kind": "fact", "content": "draft wording"})
        mid = body["id"]
        code, body = req("PATCH", f"{self.base}/memories/{mid}",
                         {"content": "final wording", "kind": "preference"})
        self.assertEqual(code, 200)
        self.assertEqual(body["memory"]["content"], "final wording")
        self.assertEqual(body["memory"]["kind"], "preference")
        code, found = req("GET", f"{self.base}/memories/search?q=final")
        self.assertTrue(any(m["id"] == mid for m in found["memories"]))
        code, found = req("GET", f"{self.base}/memories/search?q=draft")
        self.assertFalse(any(m["id"] == mid for m in found["memories"]))
        # bad kind and unknown id are rejected
        code, _ = req("PATCH", f"{self.base}/memories/{mid}", {"kind": "nope"})
        self.assertEqual(code, 400)
        code, _ = req("PATCH", f"{self.base}/memories/does-not-exist",
                      {"content": "x"})
        self.assertEqual(code, 400)

    def test_memory_review_queue_flow(self):
        code, body = req("POST", f"{self.base}/memories",
                         {"kind": "fact", "content": "inferred zebra habit",
                          "consent": "inferred-pending"})
        mid = body["id"]
        code, pending = req("GET", f"{self.base}/memories/pending")
        self.assertTrue(any(p["id"] == mid for p in pending["pending"]))
        # still hidden from normal recall until approved
        code, found = req("GET", f"{self.base}/memories/search?q=zebra")
        self.assertFalse(any(m["id"] == mid for m in found["memories"]))
        code, _ = req("POST", f"{self.base}/memories/{mid}/approve")
        self.assertEqual(code, 200)
        code, pending = req("GET", f"{self.base}/memories/pending")
        self.assertFalse(any(p["id"] == mid for p in pending["pending"]))
        code, found = req("GET", f"{self.base}/memories/search?q=zebra")
        self.assertTrue(any(m["id"] == mid for m in found["memories"]))
        # approving twice fails
        code, _ = req("POST", f"{self.base}/memories/{mid}/approve")
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
