"""额度自动切换、失败分类、会话请求头与可复现配置。"""

import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from memsys.bench.provenance import answer_model_settings, model_details
from memsys.config import AppConfig
from memsys.fallback import QuotaFallbackChat
from memsys.llm import ChatResult, ModelConfig, ModelError, OpenAICompatClient, UsageLedger
from test_llm import FakeServer, HI, chat_reply


class StubChat:
    def __init__(self, name, replies):
        self.config = ModelConfig(model=name, base_url="https://example.test/v1")
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(reply, self.config.model, 1, 1, 0, 0, 0, "stop")


class QuotaFallbackTest(unittest.TestCase):
    def test_quota_switch_is_sticky_and_preserves_request_options(self):
        primary = StubChat("a", [ModelError("limit", 402)])
        backup = StubChat("b", ['{"ok":true}', "second"])
        pool = QuotaFallbackChat([primary, backup], ["a", "b"])
        tags = {"session_id": "same-conversation", "question_id": "q1"}
        result = pool.chat(HI, purpose="extract", temperature=0.2, max_tokens=100,
                           json_mode=True, tags=tags)
        self.assertEqual((result.model, result.text, pool.active_name), ("b", '{"ok":true}', "b"))
        self.assertEqual(primary.calls[0], backup.calls[0])
        self.assertEqual(backup.calls[0][1]["tags"], tags)
        self.assertEqual(pool.chat(HI, purpose="answer").text, "second")
        self.assertEqual(len(primary.calls), 1)

    def test_all_exhausted_are_bounded_and_can_recover_after_cooldown(self):
        primary = StubChat("a", [ModelError("rate", 429), "recovered"])
        backup = StubChat("b", [ModelError("balance", 402)])
        pool = QuotaFallbackChat([primary, backup], ["a", "b"], cooldown_s=60)
        with patch("memsys.fallback.time.monotonic", return_value=10):
            with self.assertRaisesRegex(ModelError, "all configured models") as error:
                pool.chat(HI, purpose="answer")
            self.assertEqual(error.exception.status, 429)
            with self.assertRaises(ModelError):
                pool.chat(HI, purpose="answer")
        self.assertEqual((len(primary.calls), len(backup.calls)), (1, 1))
        with patch("memsys.fallback.time.monotonic", return_value=71):
            self.assertEqual(pool.chat(HI, purpose="answer").text, "recovered")

    def test_auth_request_server_and_network_errors_do_not_switch(self):
        for status in (401, 403, 400, 500, None):
            with self.subTest(status=status):
                primary = StubChat("a", [ModelError("invalid request or authentication", status)])
                backup = StubChat("b", ["unused"])
                with self.assertRaises(ModelError):
                    QuotaFallbackChat([primary, backup], ["a", "b"]).chat(HI, purpose="answer")
                self.assertEqual(backup.calls, [])

    def test_concurrent_recovery_does_not_break_exhausted_call(self):
        primary = StubChat("a", [ModelError("limit", 429), "recovered"])
        pool = QuotaFallbackChat([primary], ["a"], cooldown_s=0)
        failure_recorded, recovery_finished = threading.Event(), threading.Event()
        def await_recovery(*args):
            failure_recorded.set()
            self.assertTrue(recovery_finished.wait(2))
        with patch("memsys.fallback.log.warning", side_effect=await_recovery):
            with ThreadPoolExecutor(max_workers=1) as worker:
                failed = worker.submit(pool.chat, HI, purpose="answer")
                try:
                    self.assertTrue(failure_recorded.wait(2))
                    self.assertEqual(pool.chat(HI, purpose="answer").text, "recovered")
                finally:
                    recovery_finished.set()
                with self.assertRaisesRegex(ModelError, "all configured models") as error:
                    failed.result(timeout=2)
                self.assertEqual(error.exception.status, 429)

    def test_priority_recovers_after_cooldown_even_when_backup_is_still_available(self):
        primary = StubChat("gpt", [ModelError("limit", 402), "higher priority"])
        backup = StubChat("deepseek", ["backup", "unused"])
        pool = QuotaFallbackChat([primary, backup], ["gpt", "deepseek"], cooldown_s=60)
        with patch("memsys.fallback.time.monotonic", return_value=10):
            self.assertEqual(pool.chat(HI, purpose="answer").text, "backup")
        with patch("memsys.fallback.time.monotonic", return_value=71):
            self.assertEqual(pool.chat(HI, purpose="answer").text, "higher priority")
        self.assertEqual(pool.active_name, "gpt")
        self.assertEqual(len(backup.calls), 1)

    def test_workspace_model_access_gate_is_skipped_but_credentials_are_not(self):
        gated = StubChat("muse", [ModelError(
            "This Go model trains on request data. Allow paid endpoints that train on request data "
            "in your workspace's Privacy settings to use it.", 400)])
        backup = StubChat("deepseek", ["available"])
        pool = QuotaFallbackChat([gated, backup], ["muse", "deepseek"])
        self.assertEqual(pool.chat(HI, purpose="answer").text, "available")

    def test_explicit_quota_messages_can_use_400_or_403(self):
        for message in ("insufficient_quota", "Insufficient balance", "quota exceeded",
                        "You have reached your usage limit", "额度不足"):
            for status in (400, 403):
                self.assertTrue(ModelError(message, status).quota_or_rate_limit)

    def test_configured_roles_share_pool_and_http_429_skips_retries(self):
        server = FakeServer()
        self.addCleanup(server.close)
        server.responses = [(429, {"error": "rate limit"}, {"Retry-After": "60"}),
                            (200, chat_reply("backup"), {}), (200, chat_reply("sticky"), {})]
        config = AppConfig.from_dict({"models": {
            name: {"model": name, "base_url": server.base_url, "max_retries": 4}
            for name in ("a", "b")}, "roles": {"chat": "a", "memory": "a"},
            "fallbacks": {"a": ["b"]}})
        ledger = UsageLedger()
        models = config.build_models(ledger, env={}, probe=False)
        self.assertIs(models.chat, models.memory)
        with patch("memsys.llm.time.sleep") as sleep:
            self.assertEqual(models.memory.chat(HI, purpose="extract").text, "backup")
            self.assertEqual(models.chat.chat(HI, purpose="answer").text, "sticky")
            sleep.assert_not_called()
        self.assertEqual([body["model"] for _, _, body in server.requests], ["a", "b", "b"])
        self.assertEqual([record.model for record in ledger.records()], ["b", "b"])

    def test_retry_exhaustion_keeps_http_status(self):
        server = FakeServer()
        self.addCleanup(server.close)
        server.responses = [(429, {"error": "rate limit"}, {})]
        client = OpenAICompatClient(ModelConfig(model="a", base_url=server.base_url, max_retries=0))
        with self.assertRaises(ModelError) as error:
            client.chat(HI, purpose="answer")
        self.assertEqual(error.exception.status, 429)

    def test_opencode_session_header_is_stable_across_models_and_changes_per_session(self):
        requests = []
        def reply(request, **kwargs):
            requests.append(request)
            return io.BytesIO(json.dumps(chat_reply("OK")).encode())
        clients = [OpenAICompatClient(ModelConfig(model=name, base_url="https://opencode.ai/zen/go/v1"),
                                     api_key="test-key") for name in ("a", "b")]
        with patch("memsys.llm.urllib.request.urlopen", side_effect=reply):
            clients[0].chat(HI, purpose="query", tags={"session_id": "session-一"})
            clients[1].chat(HI, purpose="answer", tags={"session_id": "session-一"})
            clients[0].chat(HI, purpose="answer", tags={"session_id": "session-two"})
        ids = [request.get_header("X-opencode-session") for request in requests]
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[0], ids[2])
        self.assertEqual(requests[0].get_header("User-agent"), "memsys/0.1.0")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-key")

    def test_invalid_candidate_lists_and_embedding_fallbacks_fail_early(self):
        data = {"models": {name: {"model": name, "base_url": "https://example.test/v1"}
                           for name in ("a", "b")}, "roles": {}}
        for fallbacks in ({"unknown": ["b"]}, {"a": ["unknown"]}, {"a": ["a"]},
                          {"a": ["b", "b"]}, {"a": "b"}, []):
            with self.subTest(fallbacks=fallbacks), self.assertRaises(ValueError):
                AppConfig.from_dict(dict(data, fallbacks=fallbacks))
        with self.assertRaisesRegex(ValueError, "vector space"):
            AppConfig.from_dict(dict(data, roles={"embedding": "b"}, fallbacks={"a": ["b"]}))

    def test_manifest_keeps_candidate_order_without_credentials_or_price_fingerprint(self):
        clients = [OpenAICompatClient(ModelConfig(model=name, base_url="https://example.test/v1"),
                                     api_key="private-key") for name in ("a", "b")]
        details = model_details(QuotaFallbackChat(clients, ["a", "b"]))
        self.assertEqual([model["model"] for model in details["candidates"]], ["a", "b"])
        self.assertNotIn("private-key", json.dumps(details))
        self.assertNotIn("pricing", json.dumps(answer_model_settings({"chat": details})))


if __name__ == "__main__":
    unittest.main()
