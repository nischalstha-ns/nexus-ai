"""Automation backends (mouse/keyboard input).

- MockAutomation: records every action for tests, dry-runs, and demos.
  This is the default — real input backends are platform-specific and
  must be opted into explicitly.
- Documented real backends (not bundled, need the platform):
    Windows: `uiautomation` or `pyautogui` (pip install pyautogui)
    Linux:   `xdotool` via a small command backend
    macOS:   `cliclick` / AppleScript
  A PyAutoGUIBackend skeleton lives in docs; Phase 5 ships the interface,
  the mock, and the permission-gated tools so the whole loop is testable
  today and swappable on a real desktop tomorrow.
"""
from __future__ import annotations

from ..core.errors import NexusError
from ..core.logging import get_logger
from .base import AutomationBackend, InputAction

log = get_logger("perception.automation")

_ALLOWED_BUTTONS = ("left", "right", "middle")
# Conservative key vocabulary — no raw keycodes, no injection of
# OS-reserved combos (the tool layer blocks those separately).
_ALLOWED_KEYS = {
    "Enter", "Tab", "Escape", "BackSpace", "Delete",
    "Up", "Down", "Left", "Right", "Home", "End",
    "Page_Up", "Page_Down", "F1", "F2", "F3", "F4", "F5", "F6",
    "F7", "F8", "F9", "F10", "F11", "F12",
    "ctrl", "alt", "shift", "super",
}


def check_keys(keys: list[str]) -> None:
    for k in keys:
        if k not in _ALLOWED_KEYS and not (len(k) == 1 and k.isprintable()):
            raise NexusError(f"key '{k}' is not in the allowed vocabulary")
    lowered = [k.lower() for k in keys]
    if "ctrl" in lowered and "alt" in lowered and "delete" in lowered:
        raise NexusError("ctrl+alt+delete is never sent")


class MockAutomation(AutomationBackend):
    name = "mock"

    def __init__(self):
        self.actions: list[InputAction] = []

    def click(self, x: int, y: int, button: str = "left") -> None:
        if button not in _ALLOWED_BUTTONS:
            raise NexusError(f"button '{button}' not allowed")
        self.actions.append(InputAction("click", {"x": x, "y": y, "button": button}))

    def type_text(self, text: str) -> None:
        if len(text) > 10000:
            raise NexusError("type_text limited to 10000 chars per call")
        self.actions.append(InputAction("type", {"text": text}))

    def press_keys(self, keys: list[str]) -> None:
        check_keys(keys)
        self.actions.append(InputAction("key", {"keys": list(keys)}))

    def scroll(self, dx: int, dy: int) -> None:
        self.actions.append(InputAction("scroll", {"dx": dx, "dy": dy}))
