"""Screen capture backends.

- MockScreen: deterministic gradient PNG (tests, demos, headless CI).
- CommandScreenCap: shells out to an operator-configured capture tool
  (e.g. `grim -` on Wayland, `scrot -` ... no, scrot writes files;
  use `import -window root png:-` from ImageMagick, or `screencapture -x -t png`
  on macOS). The command must emit PNG bytes on stdout.
"""
from __future__ import annotations

import shlex
import subprocess

from ..core.errors import NexusError
from ..core.logging import get_logger
from .base import ScreenObserver, Screenshot, WindowInfo
from .png import decode_png_size, test_card

log = get_logger("perception.screencap")


class MockScreen(ScreenObserver):
    name = "mock"

    def __init__(self, width: int = 320, height: int = 200):
        self._w, self._h = width, height

    def capture(self, timeout_s: float = 10.0) -> Screenshot:
        png = test_card(self._w, self._h)
        return Screenshot(png_bytes=png, width=self._w, height=self._h,
                          source=self.name)

    def active_window(self) -> WindowInfo:
        return WindowInfo(title="Mock Window", app_name="mock",
                          width=self._w, height=self._h, source=self.name)


class CommandScreenCap(ScreenObserver):
    name = "command"

    def __init__(self, command: str):
        """e.g. 'screencapture -x -t png -' (macOS) or 'grim -' (Wayland)."""
        self.command = command

    def capture(self, timeout_s: float = 10.0) -> Screenshot:
        try:
            proc = subprocess.run(shlex.split(self.command), capture_output=True,
                                  timeout=timeout_s)
        except FileNotFoundError as e:
            raise NexusError(f"capture command not found: {self.command}") from e
        except subprocess.TimeoutExpired as e:
            raise NexusError("screen capture timed out") from e
        if proc.returncode != 0 or proc.stdout[:8] != b"\x89PNG\r\n\x1a\n":
            raise NexusError(
                f"capture failed: {proc.stderr.decode(errors='replace')[:200]}")
        w, h = decode_png_size(proc.stdout)
        log.info("captured %dx%d screenshot", w, h)
        return Screenshot(png_bytes=proc.stdout, width=w, height=h,
                          source=self.command)

    def active_window(self) -> WindowInfo:
        raise NexusError("active-window metadata not supported by command backend")
