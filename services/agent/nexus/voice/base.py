"""Voice interfaces. Engines are swappable; the pipeline only speaks ABCs."""
from __future__ import annotations

import abc
import threading
from dataclasses import dataclass


@dataclass
class AudioFormat:
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2  # bytes; 2 = 16-bit PCM


@dataclass
class Transcription:
    text: str
    language: str = ""
    confidence: float = 0.0


class STTEngine(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def transcribe(self, wav_bytes: bytes, timeout_s: float = 60.0) -> Transcription:
        """Speech -> text. wav_bytes must be a WAV container."""


class TTSEngine(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def synthesize(self, text: str, voice: str = "", timeout_s: float = 60.0) -> bytes:
        """Text -> WAV bytes."""

    def voices(self) -> list[str]:
        return []


class MicInput(abc.ABC):
    name = "base"
    format = AudioFormat()

    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def read_chunk(self) -> bytes:
        """Raw PCM frames (no WAV header), matching self.format."""

    @abc.abstractmethod
    def stop(self) -> None: ...


class AudioPlayer(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def play(self, wav_bytes: bytes, stop_event: threading.Event | None = None) -> bool:
        """Play to completion. Returns False if interrupted via stop_event."""
