"""Tests: Phase 5 computer tools — perception, automation, permissions, activity."""
import os
import shutil
import sqlite3
import tempfile
import unittest

from nexus.core.errors import NexusError
from nexus.core.types import Permission, ToolStatus
from nexus.perception.automation import MockAutomation, check_keys
from nexus.perception.base import TextBlock
from nexus.perception.ocr import MockOCR, TesseractOCR
from nexus.perception.png import decode_png_size, encode_png, test_card
from nexus.perception.screencap import CommandScreenCap, MockScreen
from nexus.perception.tools import ComputerContext, computer_tools
from nexus.tools.registry import ToolRegistry


def make_registry(tmp):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(open(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data",
        "migrations", "001_initial.sql")).read())
    reg = ToolRegistry(conn)
    ctx = ComputerContext(MockScreen(), MockOCR(), MockAutomation(), tmp,
                          allowed_apps={"calc": "echo calc"})
    for spec in computer_tools(ctx):
        reg.register(spec)
    return reg, ctx, conn


class PngTest(unittest.TestCase):
    def test_roundtrip_size(self):
        png = test_card(64, 48)
        self.assertEqual(decode_png_size(png), (64, 48))

    def test_bad_bytes_rejected(self):
        with self.assertRaises(ValueError):
            decode_png_size(b"not a png")

    def test_encode_size_mismatch(self):
        with self.assertRaises(AssertionError):
            encode_png(2, 2, b"\x00" * 10)


class ScreenTest(unittest.TestCase):
    def test_mock_capture(self):
        shot = MockScreen(160, 120).capture()
        self.assertEqual((shot.width, shot.height), (160, 120))
        self.assertEqual(decode_png_size(shot.png_bytes), (160, 120))

    def test_command_missing_binary(self):
        with self.assertRaises(NexusError):
            CommandScreenCap("definitely-not-a-capture-tool-xyz").capture()

    def test_mock_window(self):
        w = MockScreen().active_window()
        self.assertTrue(w.title)


class OCRTest(unittest.TestCase):
    def test_mock_ocr(self):
        blocks = MockOCR([TextBlock("hi", 0, 0, 10, 10)]).read_text(b"png")
        self.assertEqual(blocks[0].text, "hi")

    def test_tesseract_missing(self):
        # Either tesseract exists (fine) or we get the clear error.
        try:
            TesseractOCR().read_text(test_card(64, 48))
        except NexusError as e:
            self.assertIn("tesseract", str(e).lower())


class AutomationTest(unittest.TestCase):
    def test_records_actions(self):
        m = MockAutomation()
        m.click(10, 20)
        m.type_text("hi")
        m.press_keys(["ctrl", "c"])
        m.scroll(0, 3)
        kinds = [a.kind for a in m.actions]
        self.assertEqual(kinds, ["click", "type", "key", "scroll"])

    def test_bad_button_rejected(self):
        with self.assertRaises(NexusError):
            MockAutomation().click(0, 0, button="nuke")

    def test_forbidden_combo(self):
        with self.assertRaises(NexusError):
            check_keys(["ctrl", "alt", "Delete"])

    def test_unknown_key_rejected(self):
        with self.assertRaises(NexusError):
            check_keys(["F99"])


class ComputerToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg, self.ctx, self.conn = make_registry(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_capture_saves_png(self):
        r = self.reg.execute("capture_screen", {})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertTrue(os.path.isfile(r.data["path"]))
        self.assertEqual(decode_png_size(open(r.data["path"], "rb").read()),
                         (r.data["width"], r.data["height"]))

    def test_read_screen_text(self):
        r = self.reg.execute("read_screen_text", {})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertIn("blocks", r.data)

    def test_open_app_allowlisted(self):
        r = self.reg.execute("open_app", {"app": "calc"})
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertIn("pid", r.data)

    def test_open_app_not_allowlisted(self):
        r = self.reg.execute("open_app", {"app": "evil"})
        self.assertEqual(r.status, ToolStatus.ERROR)

    def test_input_tools_need_approval(self):
        # ELEVATED by default: denied without an approver.
        for name, args in [("click_at", {"x": 1, "y": 2}),
                           ("type_text", {"text": "hi"}),
                           ("press_keys", {"keys": ["Enter"]})]:
            r = self.reg.execute(name, args)
            self.assertEqual(r.status, ToolStatus.DENIED, name)
        # ...but run fine once approved.
        r = self.reg.execute("click_at", {"x": 1, "y": 2},
                             approver=lambda n, a: True, approved_by="tester")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(self.ctx.automation.actions[0].kind, "click")

    def test_scroll_is_execute(self):
        r = self.reg.execute("scroll_by", {"dx": 0, "dy": 5})
        self.assertEqual(r.status, ToolStatus.OK)


class ServicePhase5Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading, time
        from nexus.core.config import Settings
        from nexus.service.app import serve
        cls._tmp = tempfile.mkdtemp()
        os.environ["NEXUS_DATA_DIR"] = cls._tmp
        s = Settings(); s.port = 0
        cls.server = serve(s)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _api(self, method, path, body=None):
        import json, urllib.request, urllib.error
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_activity_endpoint(self):
        code, body = self._api("POST", "/chat", {"message": "hi"})
        self.assertEqual(code, 200)
        code, body = self._api("GET", "/activity?limit=10")
        self.assertEqual(code, 200)
        self.assertIn("tool_executions", body)
        self.assertIn("audit", body)

    def test_permissions_flow(self):
        code, body = self._api("GET", "/permissions")
        self.assertEqual(code, 200)
        names = [p["tool_name"] for p in body["policies"]]
        self.assertIn("click_at", names)
        lvl = next(p["level"] for p in body["policies"] if p["tool_name"] == "click_at")
        self.assertEqual(lvl, "elevated")
        code, body = self._api("POST", "/permissions/click_at", {"level": "execute"})
        self.assertEqual(code, 200)
        code, body = self._api("GET", "/permissions")
        lvl = next(p["level"] for p in body["policies"] if p["tool_name"] == "click_at")
        self.assertEqual(lvl, "execute")
        # bad level / unknown tool
        code, _ = self._api("POST", "/permissions/click_at", {"level": "nope"})
        self.assertEqual(code, 400)
        code, _ = self._api("POST", "/permissions/nope", {"level": "observe"})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
