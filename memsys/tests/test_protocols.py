"""三种 API 的消息格式、用量、鉴权和跨协议额度切换。"""

import io
import json
import unittest
from unittest.mock import patch

from memsys.config import make_client
from memsys.fallback import QuotaFallbackChat
from memsys.llm import ModelConfig, ModelError, OpenAICompatClient, Pricing, UsageLedger
from memsys.opencode_go import BASE_URL, ZEN_BASE_URL, FREE_PRIORITY, PRIORITY, configure, configure_free, model_spec, prioritized_ids
from memsys.protocols import AnthropicMessagesClient, ResponsesClient
from test_llm import FakeServer, HI, chat_reply


def response_reply(text="OK", status="completed"):
    return {"status": status, "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private thought"}]},
        {"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 1000, "output_tokens": 200,
                  "input_tokens_details": {"cached_tokens": 400}}}


def message_reply(text="OK", reason="end_turn"):
    return {"content": [{"type": "thinking", "thinking": "private thought"},
                        {"type": "text", "text": text}], "stop_reason": reason,
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 50, "cache_creation_input_tokens": 25}}


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)

    def spec(self, model="test", api="responses", **kwargs):
        return ModelConfig(model=model, base_url=self.server.base_url, api=api, **kwargs)

    def test_responses_json_messages_token_budget_and_usage(self):
        self.server.responses = [(200, response_reply('{"ok":true}'), {})]
        ledger = UsageLedger()
        config = self.spec(output_token_headroom=100, pricing=Pricing(input=2, output=8, cached_input=0.5),
                           extra_body={"reasoning": {"effort": "none"}})
        client = ResponsesClient(config, ledger, api_key="test-key")
        messages = [{"role": "system", "content": "JSON only"},
                    {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "Return JSON"}]
        result = client.chat(messages, purpose="extract", max_tokens=200, json_mode=True)
        path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        expected_messages = [dict(message) for message in messages]
        expected_messages[2]["phase"] = "final_answer"
        self.assertEqual(body["input"], expected_messages)
        self.assertNotIn("phase", messages[2])
        self.assertEqual(body["max_output_tokens"], 300)
        self.assertEqual(body["text"], {"format": {"type": "json_object"}})
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertNotIn("temperature", body)
        self.assertFalse(body["store"])
        self.assertEqual(result.text, '{"ok":true}')
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.cached_tokens), (1000, 200, 400))
        self.assertAlmostEqual(result.cost, 0.003)
        self.assertEqual(ledger.records()[0].purpose, "extract")

    def test_responses_select_final_phase_and_keep_whole_request_usage(self):
        reply = response_reply()
        reply["output"] = [
            {"type": "message", "phase": "commentary", "content": [
                {"type": "output_text", "text": '{"facts":[]} Draft explanation.'}]},
            {"type": "message", "phase": "final_answer", "content": [
                {"type": "output_text", "text": '{"facts":['},
                {"type": "output_text", "text": '{"text":"Three apprentices joined."}]}'}]},
        ]
        self.server.responses = [(200, reply, {})]
        ledger = UsageLedger()
        client = ResponsesClient(self.spec(), ledger)
        result = client.chat(HI, purpose="extract", json_mode=True)
        self.assertEqual(json.loads(result.text), {"facts": [{"text": "Three apprentices joined."}]})
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (1000, 200))
        self.assertEqual(len(ledger.records()), 1)

    def test_responses_final_refusal_does_not_leak_commentary(self):
        reply = {"status": "completed", "output": [
            {"type": "message", "phase": "commentary", "content": [
                {"type": "output_text", "text": "I might be able to answer."}]},
            {"type": "message", "phase": "final_answer", "content": [
                {"type": "refusal", "refusal": "I cannot answer."}]},
        ]}
        self.server.responses = [(200, reply, {})]
        result = ResponsesClient(self.spec()).chat(HI, purpose="answer")
        self.assertEqual((result.text, result.finish_reason), ("I cannot answer.", "refusal"))

    def test_responses_commentary_without_final_is_not_a_completed_answer(self):
        reply = response_reply("Still working")
        reply["output"][-1]["phase"] = "commentary"
        incomplete = dict(reply, status="incomplete")
        self.server.responses = [(200, reply, {}), (200, incomplete, {})]
        client = ResponsesClient(self.spec())
        with self.assertRaisesRegex(ModelError, "no response text"):
            client.chat(HI, purpose="answer")
        result = client.chat(HI, purpose="answer")
        self.assertEqual((result.text, result.finish_reason), ("", "length"))

    def test_responses_replay_preserves_explicit_assistant_phase(self):
        self.server.responses = [(200, response_reply(), {})]
        messages = [{"role": "assistant", "phase": "commentary", "content": "Checking"},
                    {"role": "assistant", "phase": "final_answer", "content": "Done"},
                    {"role": "user", "content": "What changed?"}]
        ResponsesClient(self.spec()).chat(messages, purpose="answer")
        self.assertEqual(self.server.requests[0][2]["input"], messages)

    def test_responses_multiple_finals_select_last_answer(self):
        reply = response_reply("draft")
        reply["output"][-1]["phase"] = "final_answer"
        reply["output"].append({"type": "message", "phase": "final_answer", "content": [
            {"type": "output_text", "text": "3"}]})
        self.server.responses = [(200, reply, {})]
        self.assertEqual(ResponsesClient(self.spec()).chat(HI, purpose="answer").text, "3")

    def test_responses_truncation_refusal_and_service_error(self):
        refusal = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "refusal", "refusal": "I cannot answer that."}]}]}
        self.server.responses = [(200, response_reply("{", "incomplete"), {}),
                                 (200, refusal, {}),
                                 (200, {"status": "failed", "error": {"code": "insufficient_quota"}}, {}),
                                 (200, {"status": "completed", "output": []}, {})]
        client = ResponsesClient(self.spec())
        self.assertEqual(client.chat(HI, purpose="extract").finish_reason, "length")
        result = client.chat(HI, purpose="answer")
        self.assertEqual((result.text, result.finish_reason), ("I cannot answer that.", "refusal"))
        with self.assertRaises(ModelError) as error:
            client.chat(HI, purpose="answer")
        self.assertEqual(error.exception.status, 402)
        with self.assertRaisesRegex(ModelError, "no response text"):
            client.chat(HI, purpose="answer")

    def test_responses_content_filter_and_other_incomplete_reasons_are_distinct(self):
        filtered = dict(response_reply('ANSWER: partial', 'incomplete'),
                        incomplete_details={'reason': 'content_filter'})
        interrupted = dict(response_reply('ANSWER: partial', 'incomplete'),
                           incomplete_details={'reason': 'max_messages'})
        self.server.responses = [(200, filtered, {}), (200, interrupted, {})]
        client = ResponsesClient(self.spec())
        self.assertEqual(client.chat(HI, purpose='answer').finish_reason, 'content_filter')
        self.assertEqual(client.chat(HI, purpose='answer').finish_reason, 'incomplete')

    def test_messages_shape_key_and_cache_accounting(self):
        self.server.responses = [(200, message_reply('{"facts":[]}'), {})]
        ledger = UsageLedger()
        client = AnthropicMessagesClient(self.spec(api="anthropic", output_token_headroom=400),
                                         ledger, api_key="test-key")
        result = client.chat([{"role": "system", "content": "SYS"},
                              {"role": "user", "content": "Return JSON"}],
                             purpose="extract", max_tokens=200, json_mode=True)
        path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(body["system"], "SYS")
        self.assertEqual(body["messages"], [{"role": "user", "content": "Return JSON"}])
        self.assertEqual(body["max_tokens"], 600)
        self.assertNotIn("response_format", body)
        self.assertEqual(result.text, '{"facts":[]}')
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.cached_tokens), (175, 20, 50))
        self.assertEqual(ledger.records()[0].purpose, "extract")

    def test_cross_protocol_quota_switch_uses_each_payload_and_no_retry_delay(self):
        self.server.responses = [(429, {"error": "rate limit"}, {"Retry-After": "60"}),
                                 (402, {"error": "quota exceeded"}, {}),
                                 (200, chat_reply("backup"), {})]
        ledger = UsageLedger()
        clients = [ResponsesClient(self.spec("gpt"), ledger, quota_fail_fast=True),
                   AnthropicMessagesClient(self.spec("claude", "anthropic"), ledger, quota_fail_fast=True),
                   OpenAICompatClient(self.spec("deepseek", "openai"), ledger, quota_fail_fast=True)]
        with patch("memsys.llm.time.sleep") as sleep:
            result = QuotaFallbackChat(clients, ["gpt", "claude", "deepseek"]).chat(
                HI, purpose="answer", tags={"session_id": "one-conversation"})
            sleep.assert_not_called()
        self.assertEqual(result.model, "deepseek")
        self.assertEqual([path for path, _, _ in self.server.requests],
                         ["/v1/responses", "/v1/messages", "/v1/chat/completions"])
        self.assertEqual([record.model for record in ledger.records()], ["deepseek"])
        self.assertEqual(ledger.records()[0].tags["session_id"], "one-conversation")

    def test_same_opencode_session_header_across_three_protocols(self):
        requests = []
        replies = iter([response_reply(), message_reply(), chat_reply("OK")])
        def reply(request, **kwargs):
            requests.append(request)
            return io.BytesIO(json.dumps(next(replies)).encode())
        specs = [ModelConfig(model=name, base_url=BASE_URL, api=api)
                 for name, api in (("gpt", "responses"), ("claude", "anthropic"), ("deepseek", "openai"))]
        with patch("memsys.llm.urllib.request.urlopen", side_effect=reply):
            for spec in specs:
                make_client(spec, UsageLedger(), api_key="test-key").chat(
                    HI, purpose="answer", tags={"session_id": "one-conversation"})
        self.assertEqual(len({request.get_header("X-opencode-session") for request in requests}), 1)
        self.assertEqual(requests[1].get_header("X-api-key"), "test-key")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-key")


class OpenCodeCatalogTest(unittest.TestCase):
    def test_all_detected_ids_get_exactly_one_pool_entry_and_correct_priorities(self):
        self.assertEqual(PRIORITY[:len(FREE_PRIORITY)], FREE_PRIORITY)
        self.assertEqual(len(PRIORITY), len(set(PRIORITY)))
        original = {"models": {"bge": {"model": "bge-m3", "base_url": "http://localhost:11434/v1"}},
                    "roles": {"embedding": "bge"}, "memory": {"context_budget_tokens": 999}}
        data = configure(original, list(reversed(PRIORITY)) + [PRIORITY[0]])
        primary = data["roles"]["chat"]
        names = [primary] + data["fallbacks"][primary]
        self.assertEqual([data["models"][name]["model"] for name in names], list(PRIORITY))
        self.assertEqual(data["roles"]["memory"], primary)
        self.assertEqual(data["roles"]["embedding"], "bge")
        self.assertEqual(data["memory"], {"context_budget_tokens": 999})
        self.assertEqual(data["models"][primary]["api"], "openai")
        self.assertEqual(model_spec("claude-haiku-5-5")["api"], "anthropic")
        self.assertEqual(model_spec("qwen3.8-max")["api"], "anthropic")
        self.assertEqual(model_spec("minimax-m3")["api"], "anthropic")
        self.assertEqual(model_spec("deepseek-v4.1-flash")["api"], "openai")
        self.assertEqual(prioritized_ids(["new-unknown", "gpt-6-luna"]), ["gpt-6-luna", "new-unknown"])

    def test_free_models_use_zen_and_free_candidates_only(self):
        data = configure_free({'roles': {'embedding':'bge'}, 'models': {}},
                              ['gpt-6-luna', 'ling-3.1-flash-free', 'longcat-2.5-preview-free'])
        primary = data['roles']['chat']
        self.assertEqual(data['models'][primary]['model'], 'ling-3.1-flash-free')
        self.assertEqual(data['roles']['embedding'], 'bge')
        for role in ('chat','memory','judge','query'):
            spec = data['models'][data['roles'][role]]
            self.assertEqual(spec['base_url'], ZEN_BASE_URL)
            self.assertEqual(spec['pricing']['input'], 0)
            self.assertTrue(all(data['models'][p]['model'] in FREE_PRIORITY
                                for p in data['fallbacks'][data['roles'][role]]))
        self.assertEqual(model_spec('muse-spark-1.3-contributor-free')['api'], 'responses')
        with self.assertRaises(ValueError):
            configure_free({}, ['gpt-6-luna'])

    def test_reasoning_only_models_receive_extra_budget_and_invalid_budget_fails(self):
        config = ModelConfig.from_dict(model_spec("kimi-k2.7-code"))
        self.assertGreater(config.output_token_headroom, 0)
        self.assertEqual(config.extra_body["thinking"], {"type": "enabled"})
        for headroom in (-1, True, "100"):
            with self.assertRaises(ValueError):
                ModelConfig.from_dict({"model": "gpt", "base_url": "x", "output_token_headroom": headroom})


if __name__ == "__main__":
    unittest.main()
