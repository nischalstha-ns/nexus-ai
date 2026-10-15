"""Tests: Phase 7 workflow learning — record, edit, dry-run, replay, revert."""
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
from nexus.core.types import Permission
from nexus.tools.registry import ToolRegistry, ToolSpec
from nexus.workflows.api import WorkflowService


def _migrations_sql():
    base = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                        "data", "migrations")
    out = []
    for name in ("001_initial.sql", "002_embeddings.sql", "003_workflows.sql"):
        with open(os.path.join(base, name)) as f:
            out.append(f.read())
    return "\n".join(out)


def make_wf(tmp):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_migrations_sql())
    reg = ToolRegistry(conn)
    files = {}

    def write_file(a):
        files[a["path"]] = a["content"]
        with open(a["path"], "w") as f:
            f.write(a["content"])
        return {"path": a["path"]}

    reg.register(ToolSpec(
        name="write_file", description="write",
        schema={"type": "object",
                "properties": {"path": {"type": "string"},
                               "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False},
        handler=write_file, default_permission=Permission.EXECUTE))
    reg.register(ToolSpec(
        name="zap", description="elevated demo",
        schema={"type": "object", "properties": {},
                "additionalProperties": False},
        handler=lambda a: {"zapped": True},
        default_permission=Permission.ELEVATED))
    return WorkflowService(conn, reg, tmp), reg, files


class WorkflowUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wf, self.reg, self.files = make_wf(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _task_with_writes(self):
        c = self.wf.conn
        c.execute("INSERT INTO tasks(id, title, status) VALUES (?,?,?)",
                  ("t1", "demo", "done"))
        p1 = os.path.join(self.tmp, "a.txt")
        p2 = os.path.join(self.tmp, "b.txt")
        self.reg.execute("write_file", {"path": p1, "content": "A"},
                         task_id="t1")
        self.reg.execute("write_file", {"path": p2, "content": "B"},
                         task_id="t1")
        c.commit()
        return "t1", p1, p2

    def test_record_from_task(self):
        tid, p1, p2 = self._task_with_writes()
        wf = self.wf.record_from_task(tid, "demo flow")
        self.assertEqual(len(wf["steps"]), 2)
        self.assertEqual(wf["steps"][0]["tool"], "write_file")
        self.assertEqual(wf["steps"][0]["arguments"]["path"], p1)

    def test_record_unknown_task(self):
        with self.assertRaises(NexusError):
            self.wf.record_from_task("nope", "x")

    def test_record_empty_task(self):
        self.wf.conn.execute(
            "INSERT INTO tasks(id, title, status) VALUES (?,?,?)",
            ("t9", "empty", "done"))
        with self.assertRaises(NexusError):
            self.wf.record_from_task("t9", "x")

    def test_editor_validation(self):
        wf = self.wf.create_workflow(
            "w", "", [{"tool": "write_file",
                       "arguments": {"path": "/x", "content": "y"}}])
        # bad tool
        with self.assertRaises(NexusError):
            self.wf.update_workflow(wf["id"], steps=[{"tool": "nope"}])
        # bad args
        with self.assertRaises(NexusError):
            self.wf.update_workflow(wf["id"], steps=[
                {"tool": "write_file", "arguments": {"path": "/x"}}])
        # good edit: reorder + rename
        updated = self.wf.update_workflow(
            wf["id"], name="renamed",
            steps=[{"tool": "write_file",
                    "arguments": {"path": "/y", "content": "z"}}])
        self.assertEqual(updated["name"], "renamed")
        self.assertEqual(updated["steps"][0]["arguments"]["path"], "/y")

    def test_dry_run_executes_nothing(self):
        tid, p1, p2 = self._task_with_writes()
        wf = self.wf.record_from_task(tid, "demo")
        os.remove(p1)
        result = self.wf.replay(wf["id"], dry_run=True)
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["dry_run"])
        self.assertFalse(os.path.exists(p1))  # nothing executed

    def test_replay_and_checkpoint_revert(self):
        p = os.path.join(self.tmp, "orig.txt")
        with open(p, "w") as f:
            f.write("original")
        wf = self.wf.create_workflow(
            "overwrite", "",
            [{"tool": "write_file",
              "arguments": {"path": p, "content": "changed"}}])
        result = self.wf.replay(wf["id"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(open(p).read(), "changed")
        # revert restores the original
        rev = self.wf.revert_run(result["run_id"])
        self.assertEqual(rev["restored"], [p])
        self.assertEqual(open(p).read(), "original")

    def test_revert_removes_new_files(self):
        p = os.path.join(self.tmp, "new.txt")
        wf = self.wf.create_workflow(
            "create", "",
            [{"tool": "write_file",
              "arguments": {"path": p, "content": "new"}}])
        result = self.wf.replay(wf["id"])
        self.assertTrue(os.path.exists(p))
        rev = self.wf.revert_run(result["run_id"])
        self.assertEqual(rev["removed"], [p])
        self.assertFalse(os.path.exists(p))

    def test_replay_stops_on_failure(self):
        # a step that fails at runtime: write to an impossible path
        wf = self.wf.create_workflow(
            "bad", "",
            [{"tool": "write_file",
              "arguments": {"path": "/proc/cannot/write.txt",
                            "content": "x"}},
             {"tool": "write_file",
              "arguments": {"path": os.path.join(self.tmp, "s2.txt"),
                            "content": "x"}}])
        result = self.wf.replay(wf["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_at"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "s2.txt")))

    def test_elevated_step_needs_approval(self):
        wf = self.wf.create_workflow(
            "z", "", [{"tool": "zap", "arguments": {}}])
        result = self.wf.replay(wf["id"])  # no approver
        self.assertEqual(result["status"], "awaiting_approval")
        # with approver it runs
        result = self.wf.replay(
            wf["id"], approver=lambda n, a: True, approved_by="tester")
        self.assertEqual(result["status"], "done")

    def test_delete(self):
        wf = self.wf.create_workflow(
            "w", "", [{"tool": "zap", "arguments": {}}])
        self.wf.delete_workflow(wf["id"])
        with self.assertRaises(NexusError):
            self.wf.get_workflow(wf["id"])


class WorkflowServiceTest(unittest.TestCase):
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

    def _api(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_full_http_cycle(self):
        # record a workflow by hand (no task needed for create)
        code, body = self._api("POST", "/workflows", {
            "name": "http demo", "description": "d",
            "steps": [{"tool": "get_time", "arguments": {}}]})
        self.assertEqual(code, 200, body)
        wid = body["workflow"]["id"]

        code, body = self._api("GET", "/workflows")
        self.assertTrue(any(w["id"] == wid for w in body["workflows"]))

        # editor
        code, body = self._api("PATCH", f"/workflows/{wid}",
                               {"description": "updated"})
        self.assertEqual(code, 200)
        self.assertEqual(body["workflow"]["description"], "updated")

        # dry-run replay
        code, body = self._api("POST", f"/workflows/{wid}/replay",
                               {"dry_run": True})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "done")
        self.assertTrue(body["dry_run"])

        # real replay
        code, body = self._api("POST", f"/workflows/{wid}/replay", {})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "done")
        run_id = body["run_id"]

        code, body = self._api("GET", f"/workflow-runs/{run_id}")
        self.assertEqual(code, 200)
        self.assertEqual(body["run"]["status"], "done")

        # bad create rejected
        code, body = self._api("POST", "/workflows",
                               {"name": "bad",
                                "steps": [{"tool": "nope"}]})
        self.assertEqual(code, 400)

        # delete
        code, body = self._api("DELETE", f"/workflows/{wid}")
        self.assertEqual(code, 200)
        code, _ = self._api("GET", f"/workflows/{wid}")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
