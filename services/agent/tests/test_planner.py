"""Tests: planner — bounded loop, done/input signals, loop detection, cancel."""
import os
import sqlite3
import unittest

from nexus.core.types import Message, Permission, ProviderResponse, Role, ToolStatus
from nexus.planner.engine import Planner
from nexus.providers.mock import MockProvider
from nexus.router.router import ModelRouter
from nexus.tools.builtin import builtin_tools
from nexus.tools.registry import ToolRegistry


def make_planner(script, **kw):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(open(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data",
        "migrations", "001_initial.sql")).read())
    reg = ToolRegistry(conn)
    import tempfile
    tmp = tempfile.mkdtemp()
    for spec in builtin_tools([tmp], ["echo"]):
        reg.register(spec)
    provider = MockProvider(script)
    router = ModelRouter([provider])
    planner = Planner(router, reg, max_iterations=kw.get("max_iterations", 8),
                      task_timeout_s=kw.get("task_timeout_s", 30.0))
    return planner, provider


def run_all(planner, text, **kw):
    events = list(planner.run(text, **kw))
    return events


class PlannerTest(unittest.TestCase):
    def test_done_signal(self):
        # No tool calls and plain text -> task completes with that text.
        p, _ = make_planner([ProviderResponse(text="all good")])
        events = run_all(p, "hi")
        done = events[-1]
        self.assertEqual(done.kind, "done")
        self.assertEqual(done.payload["status"], "done")

    def test_tool_then_done(self):
        from nexus.core.types import ToolCall
        prov = MockProvider([
            ProviderResponse(tool_calls=[ToolCall(name="get_time", arguments={})]),
            ProviderResponse(tool_calls=[ToolCall(
                name="planner_control",
                arguments={"action": "done", "summary": "time fetched"})]),
        ])
        planner, _ = make_planner(prov.script)
        events = run_all(planner, "what time is it")
        kinds = [e.kind for e in events]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        done = events[-1]
        self.assertEqual(done.payload["status"], "done")
        self.assertEqual(done.payload["summary"], "time fetched")

    def test_need_input_pauses(self):
        from nexus.core.types import ToolCall
        prov = MockProvider([ProviderResponse(
            tool_calls=[ToolCall(name="planner_control",
                                 arguments={"action": "need_input",
                                            "question": "which file?"})])])
        planner, _ = make_planner(prov.script)
        events = run_all(planner, "do thing")
        done = events[-1]
        self.assertEqual(done.payload["status"], "awaiting_input")
        self.assertEqual(done.payload["summary"], "which file?")

    def test_loop_detection(self):
        from nexus.core.types import ToolCall
        prov = MockProvider([ProviderResponse(
            tool_calls=[ToolCall(name="get_time", arguments={})])] * 5)
        planner, _ = make_planner(prov.script, max_iterations=8)
        events = run_all(planner, "loop me")
        done = events[-1]
        self.assertEqual(done.payload["status"], "failed")
        self.assertIn("loop detected", done.payload["summary"])

    def test_max_iterations(self):
        from nexus.core.types import ToolCall
        prov = MockProvider([ProviderResponse(
            tool_calls=[ToolCall(name="get_time", arguments={"x": i})])
            for i in range(10)])
        planner, _ = make_planner(prov.script, max_iterations=3)
        events = run_all(planner, "go")
        self.assertIn("3 iterations", events[-1].payload["summary"])

    def test_cancel(self):
        from nexus.core.types import ToolCall
        prov = MockProvider([ProviderResponse(
            tool_calls=[ToolCall(name="get_time", arguments={})])] * 10)
        planner, _ = make_planner(prov.script)
        gen = planner.run("go")
        first = next(gen)          # status event
        self.assertEqual(first.kind, "status")
        planner.cancel()           # user hits stop mid-task
        rest = list(gen)
        self.assertEqual(rest[-1].kind, "done")
        self.assertEqual(rest[-1].payload["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
