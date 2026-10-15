"""OpenAI-compatible chat provider (OpenAI, gateways, Ollama, llama.cpp server).

Stdlib-only HTTP. Supports JSON tool-call parsing from the common
`tool_calls` response shape; providers that don't support tools simply
return text and the planner falls back to text parsing.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..core.errors import ProviderAuthError, ProviderError, ProviderUnavailable
from ..core.logging import get_logger, redact
from ..core.types import ProviderResponse, ToolCall
from .base import AIProvider, HealthStatus

log = get_logger("providers.openai_compat")


class OpenAICompatibleProvider(AIProvider):
    name = "openai_compatible"

    def __init__(self, base_url: str, model_id: str, api_key: str | None = None,
                 is_local: bool = False):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._api_key = api_key
        self.is_local = is_local

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def complete(self, messages, tools=None, timeout_s=60.0, **kwargs) -> ProviderResponse:
        payload: dict = {
            "model": self.model_id,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        payload.update(kwargs)
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method="POST",
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ProviderAuthError(f"provider rejected credentials (HTTP {e.code})") from e
            raise ProviderError(f"provider HTTP {e.code}: {redact(e.read().decode(errors='replace')[:300])}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"provider unreachable: {e}") from e
        latency_ms = int((time.monotonic() - start) * 1000)

        try:
            choice = body["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"unexpected provider response shape: {str(body)[:300]}") from e

        tool_calls = []
        for tc in choice.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc["function"].get("arguments")}
            tool_calls.append(ToolCall(
                name=tc["function"]["name"], arguments=args,
                call_id=tc.get("id", "")))
        usage = body.get("usage") or {}
        return ProviderResponse(
            text=choice.get("content") or "",
            tool_calls=tool_calls,
            model=body.get("model", self.model_id),
            usage={k: int(v) for k, v in usage.items() if isinstance(v, int)},
            latency_ms=latency_ms,
        )

    def stream_complete(self, messages, tools=None, timeout_s=60.0, **kwargs):
        """Yield ProviderResponse chunks as SSE arrives (OpenAI stream format).

        Each yielded chunk carries the *delta* text in .text. The final chunk
        has tool_calls populated (if any) and complete usage.
        """
        payload: dict = {
            "model": self.model_id,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        payload.update(kwargs)
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method="POST",
        )
        start = time.monotonic()
        try:
            resp = urllib.request.urlopen(req, timeout=timeout_s)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ProviderAuthError(f"provider rejected credentials (HTTP {e.code})") from e
            raise ProviderError(f"provider HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"provider unreachable: {e}") from e

        def _events():
            buf = ""
            try:
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    buf += chunk.decode(errors="replace")
                    while "\n\n" in buf:
                        raw, buf = buf.split("\n\n", 1)
                        for line in raw.splitlines():
                            line = line.strip()
                            if line.startswith("data:"):
                                yield line[5:].strip()
            finally:
                resp.close()

        full_text: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: dict = {}
        model = self.model_id
        try:
            for data in _events():
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                model = obj.get("model", model)
                usage = obj.get("usage") or usage
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    full_text.append(piece)
                    yield ProviderResponse(text=piece, model=model)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    while len(tool_calls) <= idx:
                        tool_calls.append(ToolCall(name="", arguments={},
                                                  call_id=tc.get("id", "")))
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        tool_calls[idx].name = fn["name"]
                    arg_frag = fn.get("arguments") or ""
                    prev = tool_calls[idx].arguments.get("_frag", "")
                    tool_calls[idx].arguments["_frag"] = prev + arg_frag
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"stream interrupted: {e}") from e

        for tc in tool_calls:  # finalize fragmented JSON arguments
            frag = tc.arguments.pop("_frag", "")
            try:
                tc.arguments = json.loads(frag or "{}")
            except json.JSONDecodeError:
                tc.arguments = {"_raw": frag}
        latency_ms = int((time.monotonic() - start) * 1000)
        yield ProviderResponse(text="".join(full_text), tool_calls=tool_calls,
                               model=model,
                               usage={k: int(v) for k, v in usage.items()
                                      if isinstance(v, int)},
                               latency_ms=latency_ms)

    def health_check(self, timeout_s=5.0) -> HealthStatus:
        start = time.monotonic()
        try:
            req = urllib.request.Request(f"{self.base_url}/models", headers=self._headers())
            with urllib.request.urlopen(req, timeout=timeout_s):
                pass
            return HealthStatus(True, "ok", int((time.monotonic() - start) * 1000))
        except Exception as e:  # never raise from a health check
            return HealthStatus(False, str(e)[:200])
