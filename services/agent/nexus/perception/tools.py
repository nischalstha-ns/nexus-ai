"""Computer-control tools: screen observation, OCR, app launching, input.

Permission posture (conservative by default):
- capture_screen / read_screen_text / get_active_window: OBSERVE (read-only)
- open_app / scroll_by: EXECUTE (explicitly permitted, allowlisted)
- click_at / type_text / press_keys: ELEVATED (always need approval)

The agent must treat every observation as coming from `source` and never
claim to see something the backend did not actually return.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid

from ..core.errors import NexusError
from ..core.types import Permission
from ..tools.registry import ToolSpec
from .automation import AutomationBackend, MockAutomation
from .base import OCReader, ScreenObserver
from .ocr import MockOCR
from .screencap import MockScreen


class ComputerContext:
    def __init__(self, screen: ScreenObserver, ocr: OCReader,
                 automation: AutomationBackend, data_dir: str,
                 allowed_apps: dict[str, str] | None = None):
        self.screen = screen
        self.ocr = ocr
        self.automation = automation
        self.data_dir = data_dir
        self.allowed_apps = allowed_apps or {}
        self.capture_dir = os.path.join(data_dir, "captures")
        os.makedirs(self.capture_dir, exist_ok=True)


def computer_tools(ctx: ComputerContext) -> list[ToolSpec]:
    def capture_screen(args):
        shot = ctx.screen.capture()
        name = f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.png"
        path = os.path.join(ctx.capture_dir, name)
        with open(path, "wb") as f:
            f.write(shot.png_bytes)
        _prune(ctx.capture_dir, keep=50)
        return {"path": path, "width": shot.width, "height": shot.height,
                "source": shot.source}

    def read_screen_text(args):
        shot = ctx.screen.capture()
        blocks = ctx.ocr.read_text(shot.png_bytes)
        return {"source": shot.source,
                "blocks": [b.__dict__ for b in blocks[:200]]}

    def get_active_window(args):
        w = ctx.screen.active_window()
        return w.__dict__

    def open_app(args):
        name = args["app"]
        if name not in ctx.allowed_apps:
            raise NexusError(
                f"app '{name}' not in allowlist: {sorted(ctx.allowed_apps)}")
        cmd = ctx.allowed_apps[name].split()
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return {"app": name, "pid": proc.pid}

    def click_at(args):
        ctx.automation.click(int(args["x"]), int(args["y"]),
                             args.get("button", "left"))
        return {"ok": True}

    def type_text(args):
        ctx.automation.type_text(args["text"])
        return {"ok": True, "chars": len(args["text"])}

    def press_keys(args):
        ctx.automation.press_keys(args["keys"])
        return {"ok": True}

    def scroll_by(args):
        ctx.automation.scroll(int(args.get("dx", 0)), int(args.get("dy", 0)))
        return {"ok": True}

    S = lambda n, d, props, req=(), perm=Permission.OBSERVE, h=None: ToolSpec(
        name=n, description=d,
        schema={"type": "object", "properties": props, "required": list(req),
                "additionalProperties": False},
        handler=h, default_permission=perm)

    return [
        S("capture_screen",
          "Capture the primary display. Returns the saved PNG path and size. "
          "The agent only knows what this tool returns — never assume more.",
          {}, h=capture_screen),
        S("read_screen_text",
          "Capture the screen and OCR it into text blocks with bounding boxes.",
          {}, h=read_screen_text),
        S("get_active_window",
          "Metadata about the currently focused window (title, app, geometry).",
          {}, h=get_active_window),
        S("open_app",
          "Launch an allowlisted application by name.",
          {"app": {"type": "string"}}, ("app",),
          perm=Permission.EXECUTE, h=open_app),
        S("click_at",
          "Click at screen coordinates. Requires approval.",
          {"x": {"type": "integer"}, "y": {"type": "integer"},
           "button": {"type": "string", "enum": ["left", "right", "middle"]}},
          ("x", "y"), perm=Permission.ELEVATED, h=click_at),
        S("type_text",
          "Type text at the current focus. Requires approval.",
          {"text": {"type": "string"}}, ("text",),
          perm=Permission.ELEVATED, h=type_text),
        S("press_keys",
          "Press named keys, e.g. ['ctrl','c']. Requires approval. "
          "ctrl+alt+delete is never sent.",
          {"keys": {"type": "array"}}, ("keys",),
          perm=Permission.ELEVATED, h=press_keys),
        S("scroll_by",
          "Scroll the mouse wheel by dx/dy clicks.",
          {"dx": {"type": "integer"}, "dy": {"type": "integer"}},
          (), perm=Permission.EXECUTE, h=scroll_by),
    ]


def _prune(directory: str, keep: int) -> None:
    files = sorted(os.path.join(directory, f) for f in os.listdir(directory)
                   if f.endswith(".png"))
    for f in files[:-keep]:
        try:
            os.remove(f)
        except OSError:
            pass


def default_context(data_dir: str) -> ComputerContext:
    """Mock backends — safe default everywhere, including CI."""
    return ComputerContext(MockScreen(), MockOCR(), MockAutomation(), data_dir)
