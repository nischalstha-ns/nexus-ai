"""Tests: provider SSE streaming against a fake OpenAI-style server."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from nexus.core.types import Message, Role
from nexus.providers.openai_compat import OpenAICompatibleProvider


def sse_body(chunks):
    out = []
    for c in chunks:
        out.append("data: " + json.dumps(c) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


CHUNKS = [
    {"model": "m", "choices": [{"delta": {"content": "Hel"}}]},
    {"model": "m", "choices": [{"delta": {"content": "lo"}}]},
    {"model": "m", "choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "get", "arguments": '{"a":'}}]}}]},
    {"model": "m", "choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '1}'}}]}}]},
    {"model": "m", "usage": {"prompt_tokens": 5, "total_tokens": 9}},
]


class FakeSSE(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = sse_body(CHUNKS)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class StreamingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeSSE)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_stream_deltas_and_final(self):
        p = OpenAICompatibleProvider(f"http://127.0.0.1:{self.port}", "m", is_local=True)
        chunks = list(p.stream_complete([Message(Role.USER, "hi")]))
        deltas = [c.text for c in chunks[:-1]]
        self.assertEqual(deltas, ["Hel", "lo"])
        final = chunks[-1]
        self.assertEqual(final.text, "Hello")
        self.assertEqual(final.tool_calls[0].name, "get")
        self.assertEqual(final.tool_calls[0].arguments, {"a": 1})
        self.assertEqual(final.usage.get("total_tokens"), 9)

    def test_stream_marks_request(self):
        seen = {}

        class Spy(FakeSSE):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                seen["payload"] = json.loads(self.rfile.read(length).decode())
                body = sse_body(CHUNKS)  # respond without re-reading the body
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), Spy)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            p = OpenAICompatibleProvider(
                f"http://127.0.0.1:{srv.server_address[1]}", "m", is_local=True)
            list(p.stream_complete([Message(Role.USER, "hi")]))
            self.assertTrue(seen["payload"]["stream"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
