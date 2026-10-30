"""端到端集成测试：真实的 HTTP 客户端、配置加载和用量账本，对着本地的 OpenAI 兼容假服务跑一遍评测。

假服务内部用 HeuristicChat 和 HashEmbedder 生成内容，所以不联网、不花钱，
但从配置文件到 usage.jsonl 再到报告里的费用，走的都是正式运行时的代码路径。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memsys import prompts
from memsys.cli import main
from memsys.fake import HashEmbedder, HeuristicChat

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mini_oracle.json")
PURPOSE_BY_SYSTEM = {prompts.EXTRACT_SYSTEM: "extract", prompts.RESOLVE_SYSTEM: "resolve",
                     prompts.QUERY_SYSTEM: "query", prompts.RERANK_SYSTEM: "rerank",
                     prompts.ANSWER_SYSTEM: "answer"}
from memsys.semantic_review import RESPONSE_RULES
PURPOSE_BY_SYSTEM[RESPONSE_RULES] = "answer_review"


class FakeOpenAI:
    def __init__(self):
        self.requests = []
        chat, embedder, fake = HeuristicChat(), HashEmbedder(), self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, body))
                if self.path.endswith("/embeddings"):
                    vectors = embedder.embed(body["input"])
                    reply = {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
                             "usage": {"prompt_tokens": 10 * len(vectors), "total_tokens": 10 * len(vectors)}}
                else:
                    messages = body["messages"]
                    purpose = PURPOSE_BY_SYSTEM.get(messages[0]["content"], "judge")
                    text = chat.chat(messages, purpose=purpose).text
                    reply = {"choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": text}}],
                             "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                                       "prompt_tokens_details": {"cached_tokens": 200}}}
                raw = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class IntegrationTest(unittest.TestCase):
    def test_bench_run_with_real_clients_records_usage_and_cost(self):
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        os.environ["MEMSYS_FAKE_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "MEMSYS_FAKE_KEY", None)
        server = FakeOpenAI()
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "models": {
                    "big": {"model": "big-model", "base_url": server.base_url, "api_key_env": "MEMSYS_FAKE_KEY",
                            "pricing": {"input": 2.0, "output": 8.0, "cached_input": 0.5}},
                    "small": {"model": "small-model", "base_url": server.base_url},
                    "embed": {"model": "embed-model", "base_url": server.base_url},
                },
                "roles": {"chat": "big", "memory": "small", "embedding": "embed", "judge": "small"},
                # This HTTP fixture models the legacy writer/reader protocol;
                # semantic behavior is covered with explicit structured fixtures.
                "memory": {"min_similarity": 0.05, "semantic_validation": False},
            }
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w") as fh:
                json.dump(config, fh)
            run_dir = os.path.join(tmp, "run")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["bench", "run", "--config", config_path, "--data", FIXTURE,
                                       "--out", run_dir, "--workers", "2"]), 0)
                main(["bench", "judge", "--config", config_path, "--data", FIXTURE,
                      "--hyp", os.path.join(run_dir, "hypotheses.jsonl")])
                main(["bench", "report", "--data", FIXTURE, "--run", run_dir])

            with open(os.path.join(run_dir, "usage.jsonl")) as fh:
                usage = [json.loads(line) for line in fh]
            by_purpose = {}
            for record in usage:
                by_purpose.setdefault(record["purpose"], set()).add(record["model"])
            self.assertEqual(by_purpose["extract"], {"small-model"})
            self.assertEqual(by_purpose["query"], {"small-model"})
            self.assertEqual(by_purpose["answer"], {"big-model"})
            self.assertEqual(by_purpose["embed"], {"embed-model"})
            self.assertEqual(by_purpose["judge"], {"small-model"})
            answers = [r for r in usage if r["purpose"] == "answer"]
            self.assertEqual(len(answers), 4)
            # (800 × 2 + 200 × 0.5 + 50 × 8) / 1e6
            self.assertAlmostEqual(answers[0]["cost"], 0.0021)
            self.assertEqual({r["tags"]["question_id"] for r in answers},
                             {"ku_city", "tr_cat", "ssu_brother_abs", "ssa_bakery"})
            json_requests = [b for p, b in server.requests if p.endswith("/chat/completions")
                             and b.get("response_format")]
            self.assertTrue(json_requests)              # 抽取和查询分析用了 JSON 模式
            with open(os.path.join(run_dir, "report.md"), encoding="utf-8") as fh:
                report = fh.read()
            self.assertIn("## Token 与费用", report)
            self.assertIn("| answer | 4 | 4000 | 800 | 200 | 0.0084 |", report)


if __name__ == "__main__":
    unittest.main()
