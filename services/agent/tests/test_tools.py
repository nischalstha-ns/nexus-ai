"""Tests: tool registry — permissions, validation, sandboxing, audit."""
import os
import shutil
import sqlite3
import tempfile
import unittest

from nexus.core.errors import ToolError
from nexus.core.types import Permission, ToolStatus
from nexus.tools.builtin import builtin_tools
from nexus.tools.registry import ToolRegistry, ToolSpec


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(open(
            os.path.join(os.path.dirname(__file__), "..", "..", "..",
                         "data", "migrations", "001_initial.sql")).read())
        self.reg = ToolRegistry(self.conn)
        for spec in builtin_tools([self.tmp], ["echo"]):
            self.reg.register(spec)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_observe_tool_runs(self):
        r = self.reg.execute("get_time", {})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertIn("utc", r.data)

    def test_unknown_tool(self):
        r = self.reg.execute("nope", {})
        self.assertEqual(r.status, ToolStatus.ERROR)

    def test_validation_missing_required(self):
        r = self.reg.execute("read_file", {})
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("required", r.error)

    def test_path_traversal_blocked(self):
        r = self.reg.execute("read_file", {"path": "/etc/passwd"})
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("outside approved roots", r.error)

    def test_write_and_read_inside_root(self):
        p = os.path.join(self.tmp, "a.txt")
        w = self.reg.execute("write_file", {"path": p, "content": "hi"})
        self.assertEqual(w.status, ToolStatus.OK)
        r = self.reg.execute("read_file", {"path": p})
        self.assertEqual(r.data["content"], "hi")

    def test_command_allowlist(self):
        ok = self.reg.execute("run_command", {"command": "echo hello", "cwd": self.tmp})
        self.assertEqual(ok.status, ToolStatus.OK)
        self.assertIn("hello", ok.data["stdout"])
        bad = self.reg.execute("run_command", {"command": "rm -rf /", "cwd": self.tmp})
        self.assertEqual(bad.status, ToolStatus.ERROR)
        self.assertIn("allowlist", bad.error)

    def test_assist_never_executes(self):
        self.reg.register(ToolSpec(
            name="suggest", description="s", schema={"type": "object"},
            handler=lambda a: {"x": 1}, default_permission=Permission.ASSIST))
        r = self.reg.execute("suggest", {})
        self.assertEqual(r.status, ToolStatus.DENIED)

    def test_elevated_requires_approver(self):
        self.reg.register(ToolSpec(
            name="danger", description="d", schema={"type": "object"},
            handler=lambda a: {"x": 1}, default_permission=Permission.ELEVATED))
        denied = self.reg.execute("danger", {})
        self.assertEqual(denied.status, ToolStatus.DENIED)
        allowed = self.reg.execute("danger", {}, approver=lambda n, a: True,
                                   approved_by="test-user")
        self.assertEqual(allowed.status, ToolStatus.OK)

    def test_audit_rows_written(self):
        self.conn.execute("INSERT INTO tasks(id, title) VALUES ('t1','x')")
        self.conn.commit()
        self.reg.execute("get_time", {}, task_id="t1")
        rows = self.conn.execute("SELECT * FROM tool_executions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_name"], "get_time")
        audit = self.conn.execute("SELECT * FROM audit_events").fetchall()
        self.assertTrue(any(a["category"] == "tool" for a in audit))

    def test_policy_change(self):
        self.reg.set_policy("get_time", Permission.ELEVATED)
        r = self.reg.execute("get_time", {})
        self.assertEqual(r.status, ToolStatus.DENIED)
        with self.assertRaises(ToolError):
            self.reg.set_policy("nope", Permission.OBSERVE)


if __name__ == "__main__":
    unittest.main()
