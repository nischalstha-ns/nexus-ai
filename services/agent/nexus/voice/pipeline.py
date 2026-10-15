"""Voice pipeline: push-to-talk -> STT -> (agent text) -> TTS -> playback.

Interruptible at every stage via a threading.Event: while the agent is
speaking, the user can barge in — stop_event halts playback immediately
and the pipeline reports what happened.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from ..core.logging import get_logger
from .base import AudioPlayer, MicInput, STTEngine, TTSEngine, Transcription
from .capture import PushToTalk

log = get_logger("voice.pipeline")


@dataclass
class VoiceTurn:
    transcript: str
    reply_text: str
    reply_audio: bytes
    played_fully: bool
    interrupted: bool = False


class VoicePipeline:
    def __init__(self, stt: STTEngine, tts: TTSEngine, player: AudioPlayer,
                 mic: MicInput | None = None):
        self.stt = stt
        self.tts = tts
        self.player = player
        self.mic = mic

    def listen(self, max_seconds: float = 30.0,
               stop_event: threading.Event | None = None) -> Transcription:
        if self.mic is None:
            raise ValueError("no microphone configured")
        ptt = PushToTalk(self.mic, max_seconds=max_seconds)
        ptt.begin()
        wav_bytes = ptt.end(stop_event=stop_event)
        return self.stt.transcribe(wav_bytes)

    def speak(self, text: str, stop_event: threading.Event | None = None) -> bool:
        """Synthesize and play. Returns False if interrupted."""
        if not text.strip():
            return True
        audio = self.tts.synthesize(text)
        return self.player.play(audio, stop_event=stop_event)

    def voice_turn(self, respond: Callable[[str], str],
                   max_seconds: float = 30.0,
                   stop_event: threading.Event | None = None) -> VoiceTurn:
        """Full turn: listen -> agent responds (text) -> speak reply."""
        transcript = self.listen(max_seconds=max_seconds, stop_event=stop_event)
        log.info("heard: %s", transcript.text[:120])
        reply_text = respond(transcript.text)
        audio = self.tts.synthesize(reply_text) if reply_text.strip() else b""
        played_fully = self.player.play(audio, stop_event=stop_event) if audio else True
        return VoiceTurn(transcript=transcript.text, reply_text=reply_text,
                         reply_audio=audio, played_fully=played_fully,
                         interrupted=not played_fully)
