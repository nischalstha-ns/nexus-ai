"""Audio playback.

- MockPlayer: records what would play; honors stop_event mid-stream
  (chunked "playback" so interruption is testable and real).
- CommandPlayer: pipes WAV to an operator-configured player command
  (e.g. 'aplay', 'afplay', PowerShell SoundPlayer one-liner).
"""
from __future__ import annotations

import io
import subprocess
import threading
import wave

from ..core.errors import ProviderError
from ..core.logging import get_logger
from .base import AudioPlayer
from .wavutil import wav_info

log = get_logger("voice.player")


class MockPlayer(AudioPlayer):
    name = "mock"

    def __init__(self, chunk_ms: int = 100):
        self.chunk_ms = chunk_ms
        self.played: list[bytes] = []
        self.interrupted = False

    def play(self, wav_bytes: bytes, stop_event: threading.Event | None = None) -> bool:
        info = wav_info(wav_bytes)
        self.played.append(wav_bytes)
        frames_per_chunk = int(info["sample_rate"] * self.chunk_ms / 1000)
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            total = w.getnframes()
            pos = 0
            while pos < total:
                if stop_event is not None and stop_event.is_set():
                    self.interrupted = True
                    log.info("playback interrupted at %.1fs",
                             pos / info["sample_rate"])
                    return False
                w.readframes(min(frames_per_chunk, total - pos))
                pos += frames_per_chunk
        return True


class CommandPlayer(AudioPlayer):
    name = "command"

    def __init__(self, command: str):
        self.command = command  # e.g. "aplay" (WAV via stdin)

    def play(self, wav_bytes: bytes, stop_event: threading.Event | None = None) -> bool:
        import shlex
        try:
            proc = subprocess.Popen(shlex.split(self.command),
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except FileNotFoundError as e:
            raise ProviderError(f"player command not found: {self.command}") from e
        assert proc.stdin is not None
        # Stream in chunks so stop_event can interrupt promptly.
        chunk = 16384
        try:
            for i in range(0, len(wav_bytes), chunk):
                if stop_event is not None and stop_event.is_set():
                    proc.kill()
                    log.info("playback interrupted by user")
                    return False
                proc.stdin.write(wav_bytes[i:i + chunk])
            proc.stdin.close()
            proc.wait(timeout=30)
            return proc.returncode == 0
        except BrokenPipeError:
            return False
