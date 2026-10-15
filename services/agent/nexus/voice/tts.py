"""Text-to-speech engines.

- MockTTS: synthesizes a *valid* WAV (tone whose length scales with the text)
  so pipelines and tests run without any engine installed.
- CommandTTS: shells out to a configured TTS command (e.g. espeak-ng) that
  must write WAV to stdout. The command is operator-configured, never
  user input at request time.
"""
from __future__ import annotations

import shlex
import subprocess

from ..core.errors import ProviderError
from ..core.logging import get_logger
from .base import TTSEngine
from .wavutil import tone_wav, wav_info

log = get_logger("voice.tts")


class MockTTS(TTSEngine):
    name = "mock"

    def __init__(self, freq_hz: float = 440.0):
        self.freq_hz = freq_hz
        self.requests: list[str] = []

    def synthesize(self, text: str, voice: str = "", timeout_s: float = 60.0) -> bytes:
        if not text.strip():
            raise ProviderError("nothing to synthesize")
        self.requests.append(text)
        duration = min(0.3 + 0.05 * len(text), 10.0)  # deterministic, bounded
        return tone_wav(duration, self.freq_hz)


class CommandTTS(TTSEngine):
    name = "command"

    def __init__(self, command: str, timeout_s: float = 60.0):
        """command: e.g. 'espeak-ng --stdout -v en'. {text} is shell-quoted
        and appended if the template contains no {text} placeholder."""
        self.command = command
        self.timeout_s = timeout_s

    def synthesize(self, text: str, voice: str = "", timeout_s: float = 60.0) -> bytes:
        if not text.strip():
            raise ProviderError("nothing to synthesize")
        quoted = shlex.quote(text)
        cmd = self.command.replace("{text}", quoted)
        if "{text}" not in self.command:
            cmd = f"{self.command} {quoted}"
        try:
            proc = subprocess.run(shlex.split(cmd), capture_output=True,
                                  timeout=timeout_s or self.timeout_s)
        except FileNotFoundError as e:
            raise ProviderError(f"TTS command not found: {self.command}") from e
        except subprocess.TimeoutExpired as e:
            raise ProviderError("TTS timed out") from e
        if proc.returncode != 0:
            raise ProviderError(f"TTS failed: {proc.stderr.decode(errors='replace')[:200]}")
        out = proc.stdout
        if out[:4] != b"RIFF":
            raise ProviderError("TTS command did not emit WAV audio")
        info = wav_info(out)
        log.info("synthesized %d chars -> %.1fs audio",
                 len(text), info["frames"] / info["sample_rate"])
        return out
