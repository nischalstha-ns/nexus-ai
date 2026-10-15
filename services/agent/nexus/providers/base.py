"""Provider abstraction: every model backend speaks this interface."""
from __future__ import annotations

import abc
from dataclasses import dataclass

from ..core.types import Message, ProviderResponse


@dataclass
class HealthStatus:
    reachable: bool
    detail: str = ""
    latency_ms: int = 0


class AIProvider(abc.ABC):
    name: str = "base"
    is_local: bool = False

    @abc.abstractmethod
    def complete(self, messages: list[Message], tools: list[dict] | None = None,
                 timeout_s: float = 60.0, **kwargs) -> ProviderResponse:
        """One chat completion. May raise ProviderError subclasses."""

    def stream_complete(self, messages: list[Message], tools: list[dict] | None = None,
                        timeout_s: float = 60.0, **kwargs):
        """Yield ProviderResponse deltas as they arrive. Optional; providers
        that don't support streaming raise NotImplementedError and callers
        must fall back to complete()."""
        raise NotImplementedError(f"{self.name} does not support streaming")

    @abc.abstractmethod
    def health_check(self, timeout_s: float = 5.0) -> HealthStatus:
        """Lightweight reachability probe. Must never raise."""
