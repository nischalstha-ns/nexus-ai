"""Microphone capture + push-to-talk.

- MicInput ABC lives in base.py.
- MockMic: feeds PCM chunks from WAV bytes (tests, demos, scripted input).
- SoundDeviceMic: real capture via the optional `sounddevice` package.
  Import is lazy; without it you get a clear error, not a crash.
- PushToTalk: begin() ... end() session returning WAV bytes, with a
  maximum-duration guard and optional energy-VAD trimming of silence.
"""
from __future__ import annotations

import io
import threading
import time
import wave

from ..core.errors import NexusError
from ..core.logging import get_logger
from .base import AudioFormat, MicInput
from .wavutil import energy_vad, pcm_frames

log = get_logger("voice.capture")


class MockMic(MicInput):
    name = "mock"

    def __init__(self, wav_bytes: bytes, chunk_ms: int = 100):
        self._pcm = pcm_frames(wav_bytes)
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            self.format = AudioFormat(w.getframerate(), w.getnchannels(),
                                      w.getsampwidth())
        self._chunk = int(self.format.sample_rate * chunk_ms / 1000) \
            * self.format.channels * self.format.sample_width
        self._pos = 0
        self._open = False

    def start(self) -> None:
        self._open = True
        self._pos = 0

    def read_chunk(self) -> bytes:
        if not self._open:
            raise NexusError("mic not started")
        chunk = self._pcm[self._pos:self._pos + self._chunk]
        self._pos += self._chunk
        time.sleep(0.005)  # behave like realtime-ish capture in tests
        return chunk

    def stop(self) -> None:
        self._open = False

    @property
    def exhausted(self) -> bool:
        return self._pos >= len(self._pcm)


class SoundDeviceMic(MicInput):
    name = "sounddevice"

    def __init__(self, device: int | str | None = None,
                 fmt: AudioFormat | None = None, chunk_ms: int = 100):
        self.format = fmt or AudioFormat()
        self._device = device
        self._chunk_frames = int(self.format.sample_rate * chunk_ms / 1000)
        self._stream = None

    def start(self) -> None:
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as e:
            raise NexusError(
                "microphone capture needs the 'sounddevice' package "
                "(pip install sounddevice) or use MockMic") from e
        self._stream = sd.InputStream(
            device=self._device, channels=self.format.channels,
            samplerate=self.format.sample_rate, dtype="int16",
            blocksize=self._chunk_frames)
        self._stream.start()

    def read_chunk(self) -> bytes:
        if self._stream is None:
            raise NexusError("mic not started")
        data, _ = self._stream.read(self._chunk_frames)
        return data.tobytes()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class PushToTalk:
    """Hold-to-talk session. begin() starts capture; end() returns WAV bytes."""

    def __init__(self, mic: MicInput, max_seconds: float = 60.0,
                 trim_silence: bool = True, vad_threshold: float = 0.02):
        self.mic = mic
        self.max_seconds = max_seconds
        self.trim_silence = trim_silence
        self.vad_threshold = vad_threshold
        self._chunks: list[bytes] = []
        self._active = False
        self._stop = threading.Event()

    def begin(self) -> None:
        if self._active:
            raise NexusError("push-to-talk already active")
        self._chunks = []
        self._stop.clear()
        self.mic.start()
        self._active = True
        log.info("push-to-talk started")

    def cancel(self) -> None:
        self._stop.set()

    def end(self, stop_event: threading.Event | None = None) -> bytes:
        if not self._active:
            raise NexusError("push-to-talk not active")
        deadline = time.monotonic() + self.max_seconds
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    log.info("capture interrupted by user")
                    break
                chunk = self.mic.read_chunk()
                if not chunk:
                    if isinstance(self.mic, MockMic) and self.mic.exhausted:
                        break
                    continue
                self._chunks.append(chunk)
                if isinstance(self.mic, MockMic) and self.mic.exhausted:
                    break
        finally:
            self.mic.stop()
            self._active = False
        pcm = b"".join(self._chunks)
        if self.trim_silence:
            pcm = self._trim(pcm)
        fmt = self.mic.format
        log.info("push-to-talk ended: %.1fs captured",
                 len(pcm) / (fmt.sample_rate * fmt.channels * fmt.sample_width))
        import io
        import wave as _wave
        buf = io.BytesIO()
        with _wave.open(buf, "wb") as w:
            w.setnchannels(fmt.channels)
            w.setsampwidth(fmt.sample_width)
            w.setframerate(fmt.sample_rate)
            w.writeframes(pcm)
        return buf.getvalue()

    def _trim(self, pcm: bytes) -> bytes:
        sw = self.mic.format.sample_width
        frame = sw * self.mic.format.channels
        # drop leading/trailing silence at ~100ms granularity
        win = int(self.mic.format.sample_rate * 0.1) * frame
        start, end = 0, len(pcm)
        while start < end and not energy_vad(pcm[start:start + win], self.vad_threshold):
            start += win
        while end > start and not energy_vad(pcm[end - win:end], self.vad_threshold):
            end -= win
        return pcm[start:end]
