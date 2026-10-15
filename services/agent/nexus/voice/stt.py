"""Speech-to-text engines.

- MockSTT: scripted transcripts for tests/demos.
- WhisperAPI: talks to any OpenAI-compatible /audio/transcriptions endpoint
  (faster-whisper-server, whisper.cpp server, OpenAI, local gateways).
  Stdlib-only multipart upload.
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
import uuid

from ..core.errors import ProviderError, ProviderUnavailable
from ..core.logging import get_logger
from .base import STTEngine, Transcription

log = get_logger("voice.stt")


class MockSTT(STTEngine):
    name = "mock"

    def __init__(self, script: list[str] | None = None):
        self.script = list(script or ["mock transcription"])
        self.received: list[bytes] = []

    def transcribe(self, wav_bytes: bytes, timeout_s: float = 60.0) -> Transcription:
        self.received.append(wav_bytes)
        text = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return Transcription(text=text, confidence=1.0)


def _multipart(fields: dict[str, str], file_field: str, filename: str,
               file_bytes: bytes, file_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(f"{v}\r\n".encode())
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="{file_field}"; '
              f'filename="{filename}"\r\n'.encode())
    buf.write(f"Content-Type: {file_type}\r\n\r\n".encode())
    buf.write(file_bytes)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


class WhisperAPI(STTEngine):
    name = "whisper_api"

    def __init__(self, base_url: str, model: str = "whisper-1",
                 api_key: str | None = None, language: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.language = language

    def transcribe(self, wav_bytes: bytes, timeout_s: float = 60.0) -> Transcription:
        if not wav_bytes[:4] == b"RIFF":
            raise ProviderError("STT input must be WAV bytes")
        fields = {"model": self.model}
        if self.language:
            fields["language"] = self.language
        body, ctype = _multipart(fields, "file", "audio.wav", wav_bytes, "audio/wav")
        headers = {"Content-Type": ctype}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(f"{self.base_url}/audio/transcriptions",
                                     data=body, headers=headers, method="POST")
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise ProviderError(f"STT HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"STT unreachable: {e}") from e
        text = payload.get("text", "")
        log.info("transcribed %d chars in %.1fs", len(text), time.monotonic() - start)
        return Transcription(text=text, language=payload.get("language", ""))
