"""Tests: voice — STT/TTS engines, VAD, push-to-talk, pipeline, endpoints."""
import io
import json
import threading
import unittest
import wave

from nexus.voice.base import AudioFormat
from nexus.voice.capture import MockMic, PushToTalk
from nexus.voice.pipeline import VoicePipeline
from nexus.voice.player import MockPlayer
from nexus.voice.stt import MockSTT, WhisperAPI
from nexus.voice.tts import CommandTTS, MockTTS
from nexus.voice.wavutil import (energy_vad, pcm_frames, rms_energy, silence_wav,
                                 tone_wav, wav_info)


class WavUtilTest(unittest.TestCase):
    def test_tone_is_valid_wav(self):
        w = tone_wav(0.5)
        info = wav_info(w)
        self.assertEqual(info["sample_rate"], 16000)
        self.assertEqual(info["channels"], 1)

    def test_vad_separates_tone_from_silence(self):
        tone = pcm_frames(tone_wav(0.2))
        silence = pcm_frames(silence_wav(0.2))
        self.assertTrue(energy_vad(tone))
        self.assertFalse(energy_vad(silence))
        self.assertGreater(rms_energy(tone), rms_energy(silence))


class STTTest(unittest.TestCase):
    def test_mock_script(self):
        stt = MockSTT(["hello world"])
        tr = stt.transcribe(tone_wav(0.2))
        self.assertEqual(tr.text, "hello world")
        self.assertEqual(len(stt.received), 1)

    def test_whisper_api_against_fake_server(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen = {}

        class FakeWhisper(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                seen["ctype"] = self.headers.get("Content-Type", "")
                seen["has_wav"] = b"RIFF" in body
                seen["has_model"] = b"whisper-1" in body
                payload = json.dumps({"text": "fake transcript"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), FakeWhisper)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            stt = WhisperAPI(f"http://127.0.0.1:{srv.server_address[1]}")
            tr = stt.transcribe(tone_wav(0.2))
            self.assertEqual(tr.text, "fake transcript")
            self.assertIn("multipart/form-data", seen["ctype"])
            self.assertTrue(seen["has_wav"])
            self.assertTrue(seen["has_model"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_whisper_rejects_non_wav(self):
        from nexus.core.errors import ProviderError
        stt = WhisperAPI("http://127.0.0.1:1")
        with self.assertRaises(ProviderError):
            stt.transcribe(b"not-a-wav")


class TTSTest(unittest.TestCase):
    def test_mock_produces_valid_wav(self):
        wav = MockTTS().synthesize("hello there")
        info = wav_info(wav)
        self.assertGreater(info["frames"], 0)

    def test_mock_rejects_empty(self):
        from nexus.core.errors import ProviderError
        with self.assertRaises(ProviderError):
            MockTTS().synthesize("   ")

    def test_command_tts_missing_binary(self):
        from nexus.core.errors import ProviderError
        tts = CommandTTS("definitely-not-a-real-tts-binary-xyz")
        with self.assertRaises(ProviderError):
            tts.synthesize("hi")


class CaptureTest(unittest.TestCase):
    def test_push_to_talk_roundtrip(self):
        wav = tone_wav(0.5)
        mic = MockMic(wav)
        ptt = PushToTalk(mic, trim_silence=False)
        ptt.begin()
        out = ptt.end()
        # same audio back, still a valid WAV of ~0.5s
        info = wav_info(out)
        self.assertAlmostEqual(info["frames"] / info["sample_rate"], 0.5, places=1)

    def test_push_to_talk_trims_silence(self):
        wav = silence_wav(0.3) + tone_wav(0.3) + silence_wav(0.3)
        # concatenate raw: build combined wav manually
        import struct
        pcm = pcm_frames(silence_wav(0.3)) + pcm_frames(tone_wav(0.3)) + pcm_frames(silence_wav(0.3))
        import io as _io
        buf = _io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(pcm)
        mic = MockMic(buf.getvalue())
        ptt = PushToTalk(mic, trim_silence=True)
        ptt.begin()
        out = ptt.end()
        info = wav_info(out)
        dur = info["frames"] / info["sample_rate"]
        self.assertLess(dur, 0.6)  # leading/trailing silence trimmed
        self.assertGreater(dur, 0.15)

    def test_double_begin_rejected(self):
        from nexus.core.errors import NexusError
        mic = MockMic(tone_wav(0.2))
        ptt = PushToTalk(mic)
        ptt.begin()
        with self.assertRaises(NexusError):
            ptt.begin()
        ptt.end()


class PipelineTest(unittest.TestCase):
    def _pipe(self, heard="what time is it"):
        mic = MockMic(tone_wav(0.3))
        return VoicePipeline(MockSTT([heard]), MockTTS(), MockPlayer(), mic)

    def test_speak_plays(self):
        pipe = self._pipe()
        self.assertTrue(pipe.speak("hello"))
        self.assertEqual(len(pipe.player.played), 1)

    def test_speak_interrupted(self):
        pipe = self._pipe()
        stop = threading.Event()
        stop.set()  # interrupt before playback starts
        self.assertFalse(pipe.speak("a somewhat longer sentence to speak aloud",
                                    stop_event=stop))
        self.assertTrue(pipe.player.interrupted)

    def test_voice_turn_end_to_end(self):
        pipe = self._pipe("turn on the lights")
        turn = pipe.voice_turn(lambda t: f"heard you: {t}")
        self.assertEqual(turn.transcript, "turn on the lights")
        self.assertIn("heard you", turn.reply_text)
        self.assertTrue(turn.played_fully)
        self.assertFalse(turn.interrupted)
        # reply audio is valid WAV
        wav_info(turn.reply_audio)

    def test_listen_without_mic(self):
        pipe = VoicePipeline(MockSTT(), MockTTS(), MockPlayer(), None)
        with self.assertRaises(ValueError):
            pipe.listen()


class VoiceServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os, shutil, tempfile, time
        from nexus.core.config import Settings
        from nexus.service.app import serve
        cls._tmp = tempfile.mkdtemp()
        os.environ["NEXUS_DATA_DIR"] = cls._tmp
        s = Settings(); s.port = 0
        cls.server = serve(s)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls.server.shutdown(); cls.server.server_close()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _post(self, path, body, ctype="application/json"):
        import urllib.request, urllib.error
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method="POST",
                                   headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers.get_content_type()
        except urllib.error.HTTPError as e:
            return e.code, e.read(), "application/json"

    def test_status(self):
        import urllib.request
        with urllib.request.urlopen(self.base + "/voice/status", timeout=10) as r:
            body = json.loads(r.read().decode())
        self.assertEqual(body["stt"], "mock")
        self.assertEqual(body["tts"], "mock")

    def test_transcribe_roundtrip(self):
        code, body, _ = self._post("/voice/transcribe", tone_wav(0.3), "audio/wav")
        self.assertEqual(code, 200)
        self.assertIn("text", json.loads(body.decode()))

    def test_synthesize_returns_wav(self):
        code, body, ctype = self._post("/voice/synthesize", {"text": "hello"})
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "audio/wav")
        wav_info(body)  # raises if not valid WAV


if __name__ == "__main__":
    unittest.main()
