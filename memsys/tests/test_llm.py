"""模型适配层测试：用本地假服务器模拟 OpenAI 兼容接口，不联网、不花钱。

运行：PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memsys.llm import (BadJSONError, ModelConfig, ModelError, OpenAICompatClient, Pricing,
                        UsageLedger, UsageRecord, chat_json, parse_json, strip_reasoning)


def setUpModule():
    # 本机若设了 HTTP 代理，访问假服务器时不走代理
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


def chat_reply(content, prompt=100, completion=20, cached=0):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion,
             "total_tokens": prompt + completion}
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": usage}


class FakeServer:
    """按顺序返回预设的响应，并记下收到的每个请求。"""

    def __init__(self):
        self.responses = []  # 每项是 (状态码, 响应体, 额外响应头)
        self.requests = []   # 每项是 (路径, 请求头, 请求体)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                fake.requests.append((self.path, self.headers, json.loads(self.rfile.read(length))))
                status, body, headers = fake.responses.pop(0)
                raw = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


HI = [{"role": "user", "content": "hi"}]


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)

    def client(self, **overrides):
        return OpenAICompatClient(ModelConfig.from_dict(
            {"model": "test-model", "base_url": self.server.base_url, **overrides}))

    def test_chat_returns_text_and_records_usage_and_cost(self):
        self.server.responses.append(
            (200, chat_reply("你好", prompt=1000, completion=200, cached=400), {}))
        client = self.client(pricing={"input": 2.0, "output": 8.0, "cached_input": 0.5},
                             extra_body={"seed": 7})
        result = client.chat(HI, purpose="answer", tags={"question_id": "q1"})

        self.assertEqual(result.text, "你好")
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.cached_tokens),
                         (1000, 200, 400))
        # 600 未缓存 × 2 + 400 缓存 × 0.5 + 200 输出 × 8，单价按每百万 token
        self.assertAlmostEqual(result.cost, 0.003)
        path, _, body = self.server.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual((body["model"], body["temperature"], body["seed"]), ("test-model", 0.0, 7))
        self.assertNotIn("response_format", body)
        [rec] = client.ledger.records()
        self.assertEqual((rec.purpose, rec.retries, rec.usage_reported, rec.tags),
                         ("answer", 0, True, {"question_id": "q1"}))

    def test_api_key_comes_from_environment(self):
        os.environ["MEMSYS_TEST_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "MEMSYS_TEST_KEY", None)
        self.server.responses.append((200, chat_reply("ok"), {}))
        self.client(api_key_env="MEMSYS_TEST_KEY").chat(HI, purpose="answer")
        self.assertEqual(self.server.requests[0][1]["Authorization"], "Bearer test-key")

    def test_missing_api_key_fails_fast(self):
        os.environ.pop("MEMSYS_MISSING_KEY", None)
        with self.assertRaisesRegex(ModelError, "MEMSYS_MISSING_KEY"):
            self.client(api_key_env="MEMSYS_MISSING_KEY")

    def test_retries_after_rate_limit(self):
        self.server.responses += [(429, {"error": "slow down"}, {"Retry-After": "0"}),
                                  (200, chat_reply("ok"), {})]
        client = self.client()
        self.assertEqual(client.chat(HI, purpose="answer").text, "ok")
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(client.ledger.records()[0].retries, 1)

    def test_error_body_with_status_200_is_a_model_error(self):
        self.server.responses.append((200, {"error": {"message": "overloaded"}}, {}))
        with self.assertRaisesRegex(ModelError, "no choices"):
            self.client().chat(HI, purpose="answer")
        self.server.responses.append((200, {"error": "overloaded"}, {}))
        with self.assertRaisesRegex(ModelError, "no embeddings"):
            self.client().embed(["x"])

    def test_client_errors_are_not_retried(self):
        self.server.responses.append((400, {"error": "bad request"}, {}))
        with self.assertRaisesRegex(ModelError, "HTTP 400"):
            self.client().chat(HI, purpose="answer")
        self.assertEqual(len(self.server.requests), 1)

    def test_gives_up_after_max_retries(self):
        self.server.responses += [(503, {"error": "busy"}, {"Retry-After": "0"})] * 2
        with self.assertRaisesRegex(ModelError, "after 2 attempts"):
            self.client(max_retries=1).chat(HI, purpose="answer")
        self.assertEqual(len(self.server.requests), 2)

    def test_json_mode_sends_response_format_and_parses_fenced_reply(self):
        self.server.responses.append((200, chat_reply('```json\n{"facts": ["住在杭州"]}\n```'), {}))
        data = chat_json(self.client(), [{"role": "user", "content": "以 JSON 输出"}],
                         purpose="extract")
        self.assertEqual(data, {"facts": ["住在杭州"]})
        self.assertEqual(self.server.requests[0][2]["response_format"], {"type": "json_object"})

    def test_json_mode_can_be_disabled_for_servers_without_it(self):
        self.server.responses.append((200, chat_reply('{"ok": true}'), {}))
        chat_json(self.client(json_mode=False), [{"role": "user", "content": "JSON"}],
                  purpose="extract")
        self.assertNotIn("response_format", self.server.requests[0][2])

    def test_reasoning_block_is_stripped_from_answer(self):
        self.server.responses.append((200, chat_reply("<think>先回忆一下</think>\n两周"), {}))
        self.assertEqual(self.client().chat(HI, purpose="answer").text, "两周")

    def test_embed_batches_restores_order_and_records_each_batch(self):
        def reply(vectors, prompt):
            # 故意倒序返回，客户端应按 index 排回原顺序
            data = [{"index": i, "embedding": v} for i, v in enumerate(vectors)][::-1]
            return {"data": data, "usage": {"prompt_tokens": prompt, "total_tokens": prompt}}

        self.server.responses += [(200, reply([[1.0], [2.0]], 6), {}),
                                  (200, reply([[3.0]], 3), {})]
        client = self.client(embed_batch_size=2)
        self.assertEqual(client.embed(["a", "b", "c"]), [[1.0], [2.0], [3.0]])
        self.assertEqual([req[2]["input"] for req in self.server.requests], [["a", "b"], ["c"]])
        self.assertEqual(client.ledger.summary()["embed"]["calls"], 2)
        self.assertEqual(client.ledger.summary()["embed"]["prompt_tokens"], 9)


class OfflineTest(unittest.TestCase):
    def test_connection_failure_raises_model_error(self):
        with socket.socket() as sock:  # 找一个当前没人监听的端口
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = ModelConfig(model="m", base_url="http://127.0.0.1:%d/v1" % port, max_retries=0)
        with self.assertRaises(ModelError):
            OpenAICompatClient(config).chat(HI, purpose="answer")

    def test_parse_json_tolerates_fences_and_prose(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json("```\n[1, 2]\n```"), [1, 2])
        self.assertEqual(parse_json('结果如下：{"a": [1, {"b": 2}]} 以上'), {"a": [1, {"b": 2}]})
        self.assertEqual(parse_json('列表：[{"a": 1}, {"b": 2}]'), [{"a": 1}, {"b": 2}])
        with self.assertRaises(BadJSONError) as ctx:
            parse_json("抱歉，我无法回答")
        self.assertEqual(ctx.exception.text, "抱歉，我无法回答")

    def test_strip_reasoning_only_removes_leading_block(self):
        self.assertEqual(strip_reasoning("<think>x</think>答案"), "答案")
        self.assertEqual(strip_reasoning("答案里提到 <think> 标签"), "答案里提到 <think> 标签")

    def test_pricing_without_cache_discount(self):
        self.assertAlmostEqual(Pricing(input=1.0, output=2.0).cost(1_000_000, 500_000, 200_000), 2.0)

    def test_unknown_price_differs_from_explicit_zero(self):
        self.assertFalse(Pricing().known)
        self.assertFalse(Pricing(input=1).known)
        self.assertTrue(Pricing(input=0, output=0).known)
        configured = ModelConfig.from_dict({"model": "m", "base_url": "http://localhost/v1",
            "pricing": {"input": 1, "output": 2, "currency": "USD", "as_of": "2026-10-09",
                        "source": "https://example.com/prices"}})
        self.assertEqual(configured.pricing.currency, "USD")
        for value in (-1, float("nan"), float("inf"), "1", True):
            with self.assertRaises(ValueError):
                Pricing(input=value, output=0)

    def test_model_config_rejects_unknown_keys(self):
        with self.assertRaises(TypeError):
            ModelConfig.from_dict({"model": "m", "base_url": "u", "temprature": 0})

    def test_ledger_is_thread_safe_and_round_trips_through_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            ledger = UsageLedger(log_path=path)

            def worker(question_id):
                for _ in range(25):
                    ledger.record(UsageRecord(
                        started_at=0.0, model="m", purpose="answer", prompt_tokens=10,
                        completion_tokens=2, cached_tokens=0, cost=0.001, latency_s=0.5,
                        retries=0, usage_reported=True, tags={"question_id": question_id}))

            threads = [threading.Thread(target=worker, args=("q%d" % i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            reloaded = UsageLedger.from_jsonl(path)
            self.assertEqual(reloaded.totals()["calls"], 200)
            self.assertEqual(reloaded.totals()["prompt_tokens"], 2000)
            per_question = reloaded.summary(by="tag:question_id")
            self.assertEqual(sorted(per_question), ["q%d" % i for i in range(8)])
            self.assertAlmostEqual(per_question["q0"]["cost"], 0.025)


if __name__ == "__main__":
    unittest.main()
