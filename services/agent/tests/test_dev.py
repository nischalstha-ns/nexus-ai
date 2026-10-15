"""Tests: Phase 8 developer agent — index, patch preview/apply, test runs."""
import os
import shutil
import sqlite3
import tempfile
import unittest

from nexus.core.errors import NexusError
from nexus.core.types import ToolStatus
from nexus.dev.indexer import ProjectIndexer, python_symbols
from nexus.dev.patcher import Patcher
from nexus.dev.tools import DevContext, dev_tools
from nexus.tools.registry import ToolRegistry


def _migrations_sql():
    base = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                        "data", "migrations")
    out = []
    for name in ("001_initial.sql", "002_embeddings.sql", "003_workflows.sql",
                 "004_dev.sql"):
        with open(os.path.join(base, name)) as f:
            out.append(f.read())
    return "\n".join(out)


def make_dev(tmp):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_migrations_sql())
    reg = ToolRegistry(conn)
    ctx = DevContext(conn, [tmp], ["python", "python3", "echo"])
    for spec in dev_tools(ctx):
        reg.register(spec)
    return ctx, reg


def make_project(tmp):
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    with open(os.path.join(proj, "app.py"), "w") as f:
        f.write("def greet(name):\n    return f'hi {name}'\n\n"
                "class App:\n    def run(self):\n        return greet('x')\n")
    with open(os.path.join(proj, "test_app.py"), "w") as f:
        f.write("import unittest\nfrom app import greet\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_greet(self):\n"
                "        self.assertEqual(greet('a'), 'hi a')\n")
    return proj


class IndexerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ctx, self.reg = make_dev(self.tmp)
        self.proj = make_project(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_python_symbols(self):
        syms = python_symbols("def f():\n pass\nclass C:\n def m(self):\n  pass\n")
        names = {s["name"] for s in syms}
        self.assertTrue({"f", "C", "m"} <= names)

    def test_index_and_find(self):
        r = self.reg.execute("index_project", {"root": self.proj})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data["files"], 2)
        r = self.reg.execute("find_symbol", {"root": self.proj, "name": "greet"})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertTrue(any(m["name"] == "greet" for m in r.data["matches"]))

    def test_search_code(self):
        r = self.reg.execute("search_code",
                             {"root": self.proj, "pattern": "def greet"})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertTrue(r.data["hits"])

    def test_jail_enforced(self):
        r = self.reg.execute("index_project", {"root": "/etc"})
        self.assertEqual(r.status, ToolStatus.ERROR)


class PatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ctx, self.reg = make_dev(self.tmp)
        self.proj = make_project(self.tmp)
        self.app = os.path.join(self.proj, "app.py")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_propose_then_apply(self):
        r = self.reg.execute("propose_patch", {
            "root": self.proj,
            "edits": [{"path": self.app,
                       "old_text": "return f'hi {name}'",
                       "new_text": "return f'hello {name}'"}]})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertIn("proposed", r.data["status"])
        self.assertIn("hello {name}", r.data["diff"])
        self.assertIn("hi {name}", r.data["diff"])
        # nothing written yet
        self.assertIn("hi {name}", open(self.app).read())
        pid = r.data["id"]
        r = self.reg.execute("apply_patch", {"patch_id": pid})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertIn("hello {name}", open(self.app).read())

    def test_ambiguous_old_text_rejected(self):
        r = self.reg.execute("propose_patch", {
            "root": self.proj,
            "edits": [{"path": self.app, "old_text": "greet",
                       "new_text": "UNIQUESTRING1"}]})
        # 'greet' appears multiple times -> proposal rejected
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("matches", r.error)

    def test_stale_patch_rejected(self):
        r = self.reg.execute("propose_patch", {
            "root": self.proj,
            "edits": [{"path": self.app,
                       "old_text": "def greet(name):",
                       "new_text": "def greet(person):"}]})
        pid = r.data["id"]
        # someone edits the file out-of-band
        src = open(self.app).read().replace("def greet(name):",
                                            "def greet(who):")
        open(self.app, "w").write(src)
        r = self.reg.execute("apply_patch", {"patch_id": pid})
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("stale", r.error.lower())

    def test_discard(self):
        r = self.reg.execute("propose_patch", {
            "root": self.proj,
            "edits": [{"path": self.app,
                       "old_text": "def greet(name):",
                       "new_text": "def greet(person):"}]})
        pid = r.data["id"]
        r = self.reg.execute("discard_patch", {"patch_id": pid})
        self.assertEqual(r.status, ToolStatus.OK)
        r = self.reg.execute("apply_patch", {"patch_id": pid})
        self.assertEqual(r.status, ToolStatus.ERROR)

    def test_run_tests(self):
        self.reg.execute("index_project", {"root": self.proj})
        r = self.reg.execute("run_tests", {"root": self.proj,
                                           "command": "python3 -m unittest discover"})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertTrue(r.data["passed"])
        self.assertIn("OK", r.data["output"])

    def test_run_tests_blocked_command(self):
        r = self.reg.execute("run_tests", {"root": self.proj,
                                           "command": "rm -rf /"})
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("allowlist", r.error)


if __name__ == "__main__":
    unittest.main()
