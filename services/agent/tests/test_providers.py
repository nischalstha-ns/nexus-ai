"""Tests: provider abstraction + model router."""
import unittest

from nexus.core.errors import ProviderAuthError, ProviderUnavailable
from nexus.core.types import Message, Role, ProviderResponse
from nexus.providers.mock import MockProvider
from nexus.providers.openai_compat import OpenAICompatibleProvider
from nexus.router.router import ModelRouter


def msg(t="hi"):
    return [Message(Role.USER, t)]


class TestMockProvider(unittest.TestCase):
    def test_complete_returns_scripted(self):
        p = MockProvider([ProviderResponse(text="hello")])
        r = p.complete(msg())
        self.assertEqual(r.text, "hello")

    def test_health_never_raises(self):
        self.assertTrue(MockProvider().health_check().reachable)

    def test_script_tool_call(self):
        p = MockProvider([])
        p.script_tool_call("get_time", {})
        r = p.complete(msg())
        self.assertEqual(r.tool_calls[0].name, "get_time")


class TestRouter(unittest.TestCase):
    def test_prefers_first_healthy(self):
        r = ModelRouter([MockProvider([ProviderResponse(text="a")]),
                         MockProvider([ProviderResponse(text="b")])])
        self.assertEqual(r.complete(msg()).text, "a")

    def test_failover_on_unavailable(self):
        class Down(MockProvider):
            def complete(self, *a, **k):
                raise ProviderUnavailable("down")
        r = ModelRouter([Down(), MockProvider([ProviderResponse(text="ok")])],
                        max_retries=0)
        self.assertEqual(r.complete(msg()).text, "ok")

    def test_auth_error_fails_fast(self):
        class BadAuth(MockProvider):
            def complete(self, *a, **k):
                raise ProviderAuthError("bad key")
        r = ModelRouter([BadAuth()], max_retries=3)
        with self.assertRaises(ProviderAuthError):
            r.complete(msg())

    def test_offline_only_filters_cloud(self):
        cloud = MockProvider([ProviderResponse(text="cloud")], is_local=False)
        local = MockProvider([ProviderResponse(text="local")], is_local=True)
        r = ModelRouter([cloud, local], offline_only=True)
        self.assertEqual(r.complete(msg()).text, "local")

    def test_no_local_provider_offline(self):
        r = ModelRouter([MockProvider(is_local=False)], offline_only=True)
        with self.assertRaises(ProviderUnavailable):
            r.complete(msg())

    def test_openai_compat_unreachable(self):
        p = OpenAICompatibleProvider("http://127.0.0.1:1", "x", is_local=True)
        h = p.health_check(timeout_s=0.5)
        self.assertFalse(h.reachable)
        with self.assertRaises(ProviderUnavailable):
            p.complete(msg(), timeout_s=0.5)


if __name__ == "__main__":
    unittest.main()
