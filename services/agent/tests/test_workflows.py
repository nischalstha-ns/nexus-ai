"""Tests: Phase 6 autonomous workflows — verify, retries, tasks, approvals."""
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

from nexus.core.types import PlanStep, ToolCall, ToolStatus
from nexus.planner.engine import Planner, verify_step
from nexus.providers.base import ProviderResponse
from nexus.providers.mock import MockProvider
from nexus.router.router import ModelRouter
from nexus.tools.registry import ToolRegistry


def make_registry():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(open(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data",
        "migrations", "001_initial.sql")).read())
    return ToolRegistry(conn)


def done_call(summary="done"):
    return ToolCall(name="planner_control",
                    arguments={"action": "done", "summary": summary})


class VerifyStepTest(unittest.TestCase):
    def test_write_file_verified(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "f.txt")
            open(p, "w").write("x")
            step = PlanStep(id="s", description="w", tool="write_file",
                            arguments={"path": p})
            res = type("R", (), {"status": ToolStatus.OK, "data": {},
                                 "error": None})()
            ok, note = verify_step(step, res)
            self.assertTrue(ok)
            self.assertIn("exists", note)
        finally:
            shutil.rmtree(tmp)

    def test_write_file_missing_fails(self):
        step = PlanStep(id="s", description="w", tool="write_file",
                        arguments={"path": "/nope/missing.txt"})
        res = type("R", (), {"status": ToolStatus.OK, "data": {},
                             "error": None})()
        ok, note = verify_step(step, res)
        self.assertFalse(ok)
        self.assertIn("not found", note)

    def test_run_command_exit_code(self):
        step = PlanStep(id="s", description="c", tool="run_command",
                        arguments={"command": "x"})
        ok_r = type("R", (), {"status": ToolStatus.OK,
                              "data": {"returncode": 0}, "error": None})()
        bad_r = type("R", (), {"status": ToolStatus.OK,
                               "data": {"returncode": 1}, "error": None})()
        self.assertTrue(verify_step(step, ok_r)[0])
        self.assertFalse(verify_step(step, bad_r)[0])

    def test_tool_error_never_verifies(self):
        step = PlanStep(id="s", description="w", tool="write_file",
                        arguments={"path": "/x"})
        res = type("R", (), {"status": ToolStatus.ERROR, "data": {},
                             "error": "boom"})()
        ok, _ = verify_step(step, res)
        self.assertFalse(ok)

    def test_unknown_tool_trusts_status(self):
        step = PlanStep(id="s", description="u", tool="weird_tool",
                        arguments={})
        res = type("R", (), {"status": ToolStatus.OK, "data": {},
                             "error": None})()
        self.assertTrue(verify_step(step, res)[0])


class PlannerWorkflowTest(unittest.TestCase):
    def _planner(self, script, tools_setup=None, **kw):
        reg = make_registry()
        if tools_setup:
            tools_setup(reg)
        prov = MockProvider(script=list(script))
        router = ModelRouter([prov])
        kw.setdefault("max_iterations", 6)
        return Planner(router, reg, **kw), reg, prov

    def test_verify_failure_fed_back_to_model(self):
        from nexus.core.types import Permission
        from nexus.tools.registry import ToolSpec

        def setup(reg):
            reg.register(ToolSpec(
                name="write_file", description="fake writer",
                schema={"type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"], "additionalProperties": False},
                handler=lambda a: {"path": a["path"]},  # writes nothing!
                default_permission=Permission.EXECUTE))

        script = [ProviderResponse(
            text="", tool_calls=[ToolCall("write_file",
                                          {"path": "/nope/missing.txt"})]),
            ProviderResponse(text="", tool_calls=[done_call("gave up")])]
        planner, reg, prov = self._planner(script, setup)
        events = list(planner.run("write it"))
        results = [e for e in events if e.kind == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].payload["status"], "error")
        self.assertIn("not found", results[0].payload["verify"])
        # the model saw the verification failure in the TOOL observation
        tool_msgs = [m for m in prov.calls[-1]
                     if getattr(m, "role", None) and "tool" in str(m.role).lower()]
        self.assertTrue(any("verify failed" in (m.content or "")
                            for m in prov.calls[-1] if m.role.value == "tool"))

    def test_retry_budget_exhausted(self):
        from nexus.core.types import Permission
        from nexus.tools.registry import ToolSpec

        calls = []

        def setup(reg):
            def boom(a):
                calls.append(a)
                raise RuntimeError("always fails")
            reg.register(ToolSpec(
                name="flaky", description="always fails",
                schema={"type": "object", "properties": {},
                        "additionalProperties": False},
                handler=boom, default_permission=Permission.EXECUTE))

        script = [ProviderResponse(
            text="", tool_calls=[ToolCall("flaky", {})]) for _ in range(4)]
        planner, reg, prov = self._planner(
            script, setup, max_step_retries=0, max_iterations=4)
        events = list(planner.run("try it"))
        # executed once (budget 0 retries), then budget errors, then loop stop
        self.assertEqual(len(calls), 1)
        budget_errors = [e for e in events
                         if e.kind == "error" and "retry budget exhausted"
                         in e.payload.get("error", "")]
        self.assertTrue(budget_errors)

    def test_task_id_threaded_to_executions(self):
        from nexus.core.types import Permission
        from nexus.tools.registry import ToolSpec
        reg = make_registry()
        reg.register(ToolSpec(
            name="ping", description="ping",
            schema={"type": "object", "properties": {},
                    "additionalProperties": False},
            handler=lambda a: {"pong": True},
            default_permission=Permission.OBSERVE))
        prov = MockProvider(script=[
            ProviderResponse(text="", tool_calls=[ToolCall("ping", {})]),
            ProviderResponse(text="", tool_calls=[done_call()])])
        planner = Planner(ModelRouter([prov]), reg, max_iterations=4)
        reg._conn.execute(
            "INSERT INTO tasks(id, title, status) VALUES (?,?,?)",
            ("task-123", "ping me", "running"))
        reg._conn.commit()
        list(planner.run("ping me", task_id="task-123"))
        rows = reg._conn.execute(
            "SELECT task_id FROM tool_executions WHERE tool_name='ping'").fetchall()
        self.assertTrue(rows)
        self.assertEqual(rows[0]["task_id"], "task-123")


class InteractiveApprovalTest(unittest.TestCase):
    def _elevated_planner(self, script, **kw):
        from nexus.core.types import Permission
        from nexus.tools.registry import ToolSpec
        reg = make_registry()
        fired = []

        def zap(a):
            fired.append(a)
            return {"zapped": True}

        reg.register(ToolSpec(
            name="zap", description="elevated demo",
            schema={"type": "object", "properties": {},
                    "additionalProperties": False},
            handler=zap, default_permission=Permission.ELEVATED))
        prov = MockProvider(script=list(script))
        kw.setdefault("max_iterations", 6)
        planner = Planner(ModelRouter([prov]), reg, **kw)
        return planner, fired

    def _drain_until_approval(self, planner, gen):
        events = []
        for ev in gen:
            events.append(ev)
            if ev.kind == "approval_request":
                break
        return events

    def test_approve_resumes_and_executes(self):
        script = [ProviderResponse(text="",
                                   tool_calls=[ToolCall("zap", {})]),
                  ProviderResponse(text="",
                                   tool_calls=[done_call("zapped it")])]
        planner, fired = self._elevated_planner(script)
        gen = planner.run("zap it", interactive_approval=True, task_id="t1")
        events = self._drain_until_approval(planner, gen)
        req = next(e for e in events if e.kind == "approval_request")
        self.assertEqual(req.payload["tool"], "zap")
        self.assertEqual(req.payload["task_id"], "t1")
        self.assertEqual(fired, [])  # not executed before approval
        planner.submit_approval(True, by="tester")
        for ev in gen:
            events.append(ev)
        self.assertEqual(len(fired), 1)  # executed after approval
        decisions = [e for e in events if e.kind == "approval_decision"]
        self.assertEqual(decisions[0].payload["approved"], True)
        final = events[-1]
        self.assertEqual(final.kind, "done")
        self.assertEqual(final.payload["status"], "done")

    def test_deny_skips_execution(self):
        script = [ProviderResponse(text="",
                                   tool_calls=[ToolCall("zap", {})]),
                  ProviderResponse(text="",
                                   tool_calls=[done_call("skipped")])]
        planner, fired = self._elevated_planner(script)
        gen = planner.run("zap it", interactive_approval=True, task_id="t2")
        events = self._drain_until_approval(planner, gen)
        self.assertTrue(any(e.kind == "approval_request" for e in events))
        planner.submit_approval(False, by="tester")
        for ev in gen:
            events.append(ev)
        self.assertEqual(fired, [])  # never executed
        results = [e for e in events if e.kind == "tool_result"]
        self.assertTrue(any("denied by user" in (r.payload.get("error") or "")
                            for r in results))


class ServiceWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from nexus.core.config import Settings
        from nexus.service.app import Handler, serve
        cls._tmp = tempfile.mkdtemp()
        os.environ["NEXUS_DATA_DIR"] = cls._tmp
        s = Settings()
        s.port = 0
        cls.server = serve(s)
        cls.service = Handler.service
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
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _api_json(self, method, path, body=None):
        code, raw = self._api(method, path, body)
        return code, json.loads(raw.decode())

    def _mock_provider(self):
        for p in self.service.router.providers:
            if p.name == "mock":
                return p
        raise AssertionError("no mock provider in router")

    def _sse_events(self, raw: bytes):
        events = []
        for chunk in raw.decode().split("data: "):
            chunk = chunk.strip()
            if not chunk or chunk == "[DONE]":
                continue
            events.append(json.loads(chunk))
        return events

    def test_task_recorded(self):
        code, body = self._api_json("POST", "/chat", {"message": "hello"})
        self.assertEqual(code, 200)
        code, body = self._api_json("GET", "/tasks?limit=5")
        self.assertEqual(code, 200)
        self.assertTrue(body["tasks"])
        tid = body["tasks"][0]["id"]
        code, body = self._api_json("GET", f"/tasks/{tid}")
        self.assertEqual(code, 200)
        self.assertIn("tool_executions", body["task"])
        code, _ = self._api_json("GET", "/tasks/nope")
        self.assertEqual(code, 404)

    def test_planner_exception_marks_task_failed(self):
        from nexus.core.errors import NexusError
        mp = next(p for p in self.service.router.providers
                  if p.name == "mock")
        orig = mp.complete

        def boom(*a, **k):
            raise NexusError("provider exploded")
        mp.complete = boom
        try:
            code, body = self._api_json("POST", "/chat", {"message": "hi"})
            self.assertEqual(code, 400)
        finally:
            mp.complete = orig
        code, body = self._api_json("GET", "/tasks?limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(body["tasks"][0]["status"], "failed")

    def test_approve_flow_over_http(self):
        mp = self._mock_provider()
        mp.script_tool_call("type_text", {"text": "hello"})
        mp.script.append(ProviderResponse(
            text="", tool_calls=[done_call("typed")]))
        self.service.computer.automation.actions.clear()

        code, raw = self._api("POST", "/chat/stream", {"message": "type hello"})
        self.assertEqual(code, 200)
        events = self._sse_events(raw)
        req = next(e for e in events if e["kind"] == "approval_request")
        tid = req["payload"]["task_id"]
        self.assertEqual(req["payload"]["tool"], "type_text")

        code, raw = self._api("POST", f"/tasks/{tid}/approve",
                              {"approved": True, "by": "tester"})
        self.assertEqual(code, 200)
        events = self._sse_events(raw)
        kinds = [e["kind"] for e in events]
        self.assertIn("approval_decision", kinds)
        self.assertIn("done", kinds)
        # the elevated tool really ran after approval
        kinds_act = [a.kind for a in self.service.computer.automation.actions]
        self.assertIn("type", kinds_act)

        # task row completed
        code, body = self._api_json("GET", f"/tasks/{tid}")
        self.assertEqual(body["task"]["status"], "done")

    def test_approve_unknown_task_404(self):
        code, _ = self._api("POST", "/tasks/nope/approve", {"approved": True})
        self.assertEqual(code, 404)

    def test_cancel_suspended_task(self):
        mp = self._mock_provider()
        mp.script_tool_call("type_text", {"text": "never"})
        code, raw = self._api("POST", "/chat/stream", {"message": "type x"})
        events = self._sse_events(raw)
        tid = next(e for e in events
                   if e["kind"] == "approval_request")["payload"]["task_id"]
        code, body = self._api_json("POST", f"/tasks/{tid}/cancel")
        self.assertEqual(code, 200)
        self.assertEqual(body["ok"], True)
        # approving afterwards is gone
        code, _ = self._api("POST", f"/tasks/{tid}/approve",
                            {"approved": True})
        self.assertEqual(code, 404)
        code, body = self._api_json("GET", f"/tasks/{tid}")
        self.assertEqual(body["task"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
