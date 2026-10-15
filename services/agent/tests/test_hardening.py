"""Tests: Phase 10 — backup/restore, request body limits."""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from nexus.core.errors import NexusError
from nexus.service import backup as backup_mod


def _migrations_sql():
    base = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                        "data", "migrations")
    out = []
    for name in ("001_initial.sql", "002_embeddings.sql", "003_workflows.sql",
                 "004_dev.sql", "005_companion.sql"):
        with open(os.path.join(base, name)) as f:
            out.append(f.read())
    return "\n".join(out)


class BackupUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_migrations_sql())
        self.conn.execute(
            "INSERT INTO conversations(id, title) VALUES (?,?)", ("c1", "t"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp)

    def test_backup_and_restore(self):
        m = backup_mod.backup(self.conn, self.tmp, label="test")
        self.assertTrue(m["name"].endswith(".db"))
        self.assertEqual(m["row_counts"]["conversations"], 1)
        # wipe live, restore
        self.conn.execute("DELETE FROM conversations")
        r = backup_mod.restore(self.conn, self.tmp, m["name"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["row_counts"]["conversations"], 1)

    def test_restore_rejects_garbage(self):
        bad = os.path.join(self.tmp, "backups", "evil.db")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "w") as f:
            f.write("not a database")
        with self.assertRaises(NexusError):
            backup_mod.restore(self.conn, self.tmp, "evil.db")

    def test_restore_rejects_path_traversal(self):
        with self.assertRaises(NexusError):
            backup_mod.restore(self.conn, self.tmp, "../evil.db")

    def test_list_backups(self):
        backup_mod.backup(self.conn, self.tmp)
        lst = backup_mod.list_backups(self.tmp)
        self.assertEqual(len(lst), 1)


class HardeningServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from nexus.core.config import Settings
        from nexus.service.app import serve
        cls._tmp = tempfile.mkdtemp()
        os.environ["NEXUS_DATA_DIR"] = cls._tmp
        s = Settings()
        s.port = 0
        cls.server = serve(s)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _api(self, method, path, body=None, raw_body=None):
        data = raw_body if raw_body is not None else (
            json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data,
                                   method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_backup_endpoints(self):
        code, body = self._api("POST", "/backup", {"label": "svc"})
        self.assertEqual(code, 200, body)
        name = body["backup"]["name"]
        code, body = self._api("GET", "/backups")
        self.assertTrue(any(b["name"] == name for b in body["backups"]))
        code, body = self._api("POST", "/restore", {"name": name})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        code, _ = self._api("POST", "/restore", {"name": "nope.db"})
        self.assertEqual(code, 400)

    def test_body_too_large_rejected(self):
        # claim a gigantic JSON body; the handler must refuse with 413
        # without reading it all
        r = urllib.request.Request(
            self.base + "/chat", data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json",
                     "Content-Length": str(50 * 1024 * 1024)})
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 413)


if __name__ == "__main__":
    unittest.main()
