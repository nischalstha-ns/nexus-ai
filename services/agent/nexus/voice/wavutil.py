"""WAV helpers (stdlib only): build, inspect, and measure PCM audio."""
from __future__ import annotations

import io
import math
import struct
import wave

from .base import AudioFormat


def make_wav(samples: list[int], fmt: AudioFormat | None = None) -> bytes:
    fmt = fmt or AudioFormat()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(fmt.channels)
        w.setsampwidth(fmt.sample_width)
        w.setframerate(fmt.sample_rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


def tone_wav(duration_s: float, freq_hz: float = 440.0,
             fmt: AudioFormat | None = None, amplitude: float = 0.5) -> bytes:
    fmt = fmt or AudioFormat()
    n = int(duration_s * fmt.sample_rate)
    samples = [int(amplitude * 32767 * math.sin(2 * math.pi * freq_hz * i / fmt.sample_rate))
               for i in range(n)]
    return make_wav(samples, fmt)


def silence_wav(duration_s: float, fmt: AudioFormat | None = None) -> bytes:
    fmt = fmt or AudioFormat()
    return make_wav([0] * int(duration_s * fmt.sample_rate), fmt)


def wav_info(wav_bytes: bytes) -> dict:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return {"channels": w.getnchannels(), "sample_width": w.getsampwidth(),
                "sample_rate": w.getframerate(), "frames": w.getnframes()}


def pcm_frames(wav_bytes: bytes) -> bytes:
    """Strip the WAV header, return raw PCM frames."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.readframes(w.getnframes())


def rms_energy(pcm: bytes, sample_width: int = 2) -> float:
    """Root-mean-square energy in [0, 1]."""
    if not pcm:
        return 0.0
    n = len(pcm) // sample_width
    samples = struct.unpack(f"<{n}h", pcm[:n * sample_width])
    return math.sqrt(sum(s * s for s in samples) / n) / 32768.0


def energy_vad(pcm_chunk: bytes, threshold: float = 0.02) -> bool:
    """True when the chunk looks like speech rather than silence."""
    return rms_energy(pcm_chunk) > threshold
