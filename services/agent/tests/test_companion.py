"""Tests: Phase 9 companion — pairing, token auth, notifications, approvals."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from nexus.providers.base import ProviderResponse
from nexus.core.types import ToolCall


def done_call(summary="done"):
    return ToolCall(name="planner_control",
                    arguments={"action": "done", "summary": summary})


class CompanionTest(unittest.TestCase):
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

    def _api(self, method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = urllib.request.Request(self.base + path, data=data,
                                   method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _pair(self, name="test-phone"):
        code, body = self._api("POST", "/pairing/code")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["code"]), 8)
        code, body = self._api("POST", "/pairing/redeem",
                               {"code": body["code"], "device_name": name})
        self.assertEqual(code, 200)
        self.assertIn("device_token", body)
        return body["device_token"], body["device_id"]

    def test_pairing_flow(self):
        token, did = self._pair()
        # code is single-use
        code, body = self._api("POST", "/pairing/code")
        c2 = body["code"]
        self._api("POST", "/pairing/redeem",
                   {"code": c2, "device_name": "x"})
        code, body = self._api("POST", "/pairing/redeem",
                               {"code": c2, "device_name": "y"})
        self.assertEqual(code, 400)
        # bad code
        code, _ = self._api("POST", "/pairing/redeem",
                            {"code": "ZZZZZZZZ", "device_name": "z"})
        self.assertEqual(code, 400)
        # device listed
        code, body = self._api("GET", "/devices")
        self.assertTrue(any(d["id"] == did for d in body["devices"]))
        # revoke
        code, body = self._api("DELETE", f"/devices/{did}")
        self.assertEqual(code, 200)
        code, _ = self._api("GET", "/companion/status", token=token)
        self.assertEqual(code, 401)

    def test_companion_auth_required(self):
        code, _ = self._api("GET", "/companion/status")
        self.assertEqual(code, 401)
        code, _ = self._api("GET", "/companion/status", token="bogus")
        self.assertEqual(code, 401)

    def test_status_and_notifications(self):
        token, _ = self._pair("phone-2")
        code, body = self._api("GET", "/companion/status", token=token)
        self.assertEqual(code, 200)
        self.assertIn("pending_approvals", body)
        nid = self.service.companion.notify("info", "hello", "world")
        code, body = self._api("GET", "/companion/notifications", token=token)
        self.assertTrue(any(n["id"] == nid for n in body["notifications"]))
        code, body = self._api(
            "POST", f"/companion/notifications/{nid}/read", {}, token=token)
        self.assertEqual(code, 200)
        row = self.service.companion.conn.execute(
            "SELECT read_at FROM notifications WHERE id=?", (nid,)).fetchone()
        self.assertIsNotNone(row["read_at"])

    def _sse_events(self, raw: bytes):
        events = []
        for chunk in raw.decode().split("data: "):
            chunk = chunk.strip()
            if not chunk or chunk == "[DONE]":
                continue
            events.append(json.loads(chunk))
        return events

    def test_companion_approval_flow(self):
        token, _ = self._pair("phone-3")
        mp = next(p for p in self.service.router.providers if p.name == "mock")
        mp.script_tool_call("type_text", {"text": "hi"})
        mp.script.append(ProviderResponse(text="",
                                          tool_calls=[done_call("typed")]))
        self.service.computer.automation.actions.clear()

        # start an interactive stream, read until it suspends
        r = urllib.request.Request(
            self.base + "/chat/stream",
            data=json.dumps({"message": "type hi"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read()
        events = self._sse_events(raw)
        tid = next(e for e in events
                   if e["kind"] == "approval_request")["payload"]["task_id"]

        # companion sees the pending approval + notification
        code, body = self._api("GET", "/companion/approvals", token=token)
        self.assertEqual(code, 200)
        self.assertTrue(any(a["id"] == tid for a in body["approvals"]))
        code, body = self._api("GET", "/companion/notifications", token=token)
        self.assertTrue(any(n["kind"] == "approval"
                            for n in body["notifications"]))

        # approve from the companion; turn resumes in background
        code, body = self._api("POST", f"/companion/approvals/{tid}",
                               {"approved": True}, token=token)
        self.assertEqual(code, 200)
        deadline = time.time() + 10
        while time.time() < deadline:
            row = self.service.store.conn.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
            if row and row["status"] == "done":
                break
            time.sleep(0.2)
        self.assertEqual(row["status"], "done")
        kinds = [a.kind for a in self.service.computer.automation.actions]
        self.assertIn("type", kinds)

        # approving again is gone
        code, _ = self._api("POST", f"/companion/approvals/{tid}",
                            {"approved": True}, token=token)
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
