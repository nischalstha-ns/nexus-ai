"""Perception interfaces: screen capture, OCR, window info, automation.

Backends are platform-specific; the agent only speaks these ABCs.
Every observation is labeled with its source so the agent can distinguish
what it actually saw from what it inferred.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class Screenshot:
    png_bytes: bytes
    width: int
    height: int
    source: str  # backend name, e.g. "mock", "grim", "screencapture"


@dataclass
class TextBlock:
    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float = 0.0


@dataclass
class WindowInfo:
    title: str
    app_name: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    source: str = ""


class ScreenObserver(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def capture(self, timeout_s: float = 10.0) -> Screenshot:
        """Capture the primary display. Raises on failure — never fakes one."""

    @abc.abstractmethod
    def active_window(self) -> WindowInfo:
        """Metadata about the focused window (no pixels)."""


class OCReader(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def read_text(self, png_bytes: bytes, timeout_s: float = 30.0) -> list[TextBlock]:
        """OCR a PNG screenshot into text blocks with bounding boxes."""


@dataclass
class InputAction:
    kind: str  # click | double_click | right_click | type | key | scroll
    params: dict = field(default_factory=dict)


class AutomationBackend(abc.ABC):
    """Sends real input events. Implementations are platform-specific;
    MockAutomation records actions for tests and dry-runs."""
    name = "base"

    @abc.abstractmethod
    def click(self, x: int, y: int, button: str = "left") -> None: ...

    @abc.abstractmethod
    def type_text(self, text: str) -> None: ...

    @abc.abstractmethod
    def press_keys(self, keys: list[str]) -> None:
        """Named keys, e.g. ['ctrl', 'c'], ['alt', 'Tab'], ['Enter']."""

    @abc.abstractmethod
    def scroll(self, dx: int, dy: int) -> None: ...
