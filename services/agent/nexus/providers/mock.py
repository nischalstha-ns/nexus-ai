"""Deterministic mock provider for tests and offline demos.

Scripted: pop responses in order; when exhausted, repeats the last one.
"""
from __future__ import annotations

from ..core.types import Message, ProviderResponse, ToolCall
from .base import AIProvider, HealthStatus


class MockProvider(AIProvider):
    name = "mock"

    def __init__(self, script: list[ProviderResponse] | None = None, is_local: bool = True):
        self.script = list(script or [])
        self.fallback = ProviderResponse(text="mock reply")
        self.is_local = is_local
        self.calls: list[list[Message]] = []

    def complete(self, messages, tools=None, timeout_s=60.0, **kwargs) -> ProviderResponse:
        self.calls.append(list(messages))
        if self.script:
            return self.script.pop(0)
        return self.fallback

    def health_check(self, timeout_s=5.0) -> HealthStatus:
        return HealthStatus(True, "mock always healthy")

    def script_tool_call(self, name: str, arguments: dict, text: str = "") -> None:
        self.script.append(ProviderResponse(
            text=text, tool_calls=[ToolCall(name=name, arguments=arguments)]))
