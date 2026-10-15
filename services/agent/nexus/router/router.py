"""Model router: picks the right provider per task, with retries and failover.

Policy:
- If offline_only (or task requires local): only consider local providers.
- Prefer providers in registration order; on ProviderUnavailable, fail over
  to the next candidate. Auth errors are NOT retried (fail fast, actionable).
- Timeouts + bounded retries with a small backoff.
"""
from __future__ import annotations

import time

from ..core.errors import ProviderAuthError, ProviderError, ProviderUnavailable
from ..core.logging import get_logger
from ..core.types import Message, ProviderResponse
from ..providers.base import AIProvider

log = get_logger("router")


class ModelRouter:
    def __init__(self, providers: list[AIProvider], offline_only: bool = False,
                 timeout_s: float = 60.0, max_retries: int = 2):
        if not providers:
            raise ValueError("router needs at least one provider")
        self.providers = providers
        self.offline_only = offline_only
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def candidates(self, require_local: bool = False) -> list[AIProvider]:
        local_only = self.offline_only or require_local
        return [p for p in self.providers if (p.is_local or not local_only)]

    def complete(self, messages: list[Message], tools: list[dict] | None = None,
                 require_local: bool = False, **kwargs) -> ProviderResponse:
        cands = self.candidates(require_local)
        if not cands:
            raise ProviderUnavailable("no providers available (offline_only with no local provider?)")
        last: Exception | None = None
        for provider in cands:
            for attempt in range(self.max_retries + 1):
                try:
                    return provider.complete(messages, tools=tools,
                                              timeout_s=self.timeout_s, **kwargs)
                except ProviderAuthError:
                    log.error("auth failed for provider '%s' — not retrying", provider.name)
                    raise
                except (ProviderUnavailable, ProviderError) as e:
                    last = e
                    log.warning("provider '%s' attempt %d failed: %s",
                                provider.name, attempt + 1, e)
                    time.sleep(min(0.25 * (2 ** attempt), 2.0))
        raise ProviderUnavailable(f"all providers failed; last error: {last}")
