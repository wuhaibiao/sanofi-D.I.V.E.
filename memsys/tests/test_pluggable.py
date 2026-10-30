"""可插拔与降级：没有模型也能跑（L0）、补抽事实（rebuild）、recall 接口、自动找模型、各种模型适配器。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from memsys.anthropic_client import AnthropicClient
from memsys.config import AppConfig, Models
from memsys.core import MemorySystem
from memsys.discover import (HostModels, config_from_discovery, discovered_configs, ollama_models,
                             resolve_models)
from memsys.fake import HashEmbedder, HeuristicChat, ScriptedChat
from memsys.llm import FunctionChat, FunctionEmbedder, ModelConfig, ModelError, UsageLedger
from memsys.schema import FACT_CURRENT

from helpers import decisions_json, dt, fact, facts_json, settings

NO_OLLAMA = {"OLLAMA_HOST": "http://127.0.0.1:9"}


def l0_models() -> Models:
    return Models(chat=None, memory=None, embedder=HashEmbedder(), embedding_name="hash-256",
                  semantic_embedding=False)


def session(system: MemorySystem, user: str, at: str, *turns: str):
    handle = system.open_session(user, dt(at))
    for i, text in enumerate(turns):
        system.add_turn(handle, "user" if i % 2 == 0 else "assistant", text, dt(at))
    return system.close_session(handle, dt(at))[0]


class DegradedTest(unittest.TestCase):
    def test_l0_stores_episodes_and_recalls_by_keywords(self):
        system = MemorySystem(l0_models(), settings())
        report = session(system, "u", "2024-01-05T10:00:00",
                         "I just adopted a beagle named Max from the shelter.", "Congratulations on Max!")
        self.assertEqual(system.models.tier, "L0")
        self.assertEqual(report.deferred, 1)
        self.assertIn("rebuild", report.summary())
        store = system.store("u")
        self.assertEqual(store.facts(), [])
        self.assertEqual(store.backlog_count(), 1)
        rec = system.recall("u", "What is the name of my beagle?", dt("2024-02-01T09:00:00"))
        self.assertEqual(rec.trace["plan"]["source"], "heuristic")
        self.assertEqual(rec.trace["tier"], "L0")
        self.assertIn("Max", rec.memory)
        self.assertEqual([i["key"] for i in rec.items], ["E1"])
        with self.assertRaises(ModelError):
            system.respond("u", "hi", dt("2024-02-01T09:00:00"))

    def test_rebuild_extracts_backlog_in_time_order(self):
        system = MemorySystem(l0_models(), settings())
        session(system, "u", "2024-01-05T10:00:00", "I live in Boston and work at a bakery.")
        session(system, "u", "2024-03-05T10:00:00", "I moved to Seattle last week.")
        memory = ScriptedChat({
            "extract": [facts_json(fact("User lives in Boston.", [1], attribute="home_city", value="Boston")),
                        facts_json(fact("User moved to Seattle.", [1], attribute="home_city", value="Seattle"))],
            "resolve": [decisions_json(("N1", "update", "F1"))]})
        system.models = Models(chat=None, memory=memory, embedder=HashEmbedder(), embedding_name="hash-256")
        reports = system.rebuild("u")
        self.assertEqual(len(reports), 2)
        store = system.store("u")
        self.assertEqual(store.backlog_count(), 0)
        boston, seattle = store.facts()
        self.assertEqual(boston.superseded_by, seattle.id)
        self.assertEqual(seattle.status, FACT_CURRENT)
        self.assertEqual(system.rebuild("u"), [])     # 再跑一次没有可补的

    def test_backfilled_older_fact_cannot_override_newer_state(self):
        system = MemorySystem(l0_models(), settings())
        session(system, "u", "2024-01-05T10:00:00", "I live in Boston.")
        memory = ScriptedChat({
            "extract": [facts_json(fact("User moved to Seattle.", [1], attribute="home_city", value="Seattle")),
                        facts_json(fact("User lives in Boston.", [1], attribute="home_city", value="Boston"))],
            "resolve": [decisions_json(("N1", "update", "F1"))]})
        system.models = Models(chat=None, memory=memory, embedder=HashEmbedder(), embedding_name="hash-256")
        session(system, "u", "2024-03-05T10:00:00", "I moved to Seattle.")
        reports = system.rebuild("u")
        self.assertEqual(reports[0].historical, 1)
        seattle = system.store("u").fact(1)
        self.assertEqual(seattle.status, FACT_CURRENT)
        self.assertIsNone(seattle.superseded_by)

    def test_rebuild_without_model_is_refused(self):
        system = MemorySystem(l0_models(), settings())
        with self.assertRaises(ModelError):
            system.rebuild("u")

    def test_only_extract_model_configured_keeps_everything_as_new(self):
        extract = ScriptedChat({"extract": [facts_json(fact("User lives in Boston.", [1], attribute="home_city")),
                                            facts_json(fact("User lives in Denver.", [1], attribute="home_city"))]})
        models = Models(chat=None, memory=None, embedder=HashEmbedder(), embedding_name="hash-256",
                        by_purpose={"extract": extract})
        self.assertEqual(models.tier, "L2")
        system = MemorySystem(models, settings())
        session(system, "u", "2024-01-05T10:00:00", "I live in Boston.")
        session(system, "u", "2024-02-05T10:00:00", "I live in Denver.")
        self.assertEqual([f.status for f in system.store("u").facts()], [FACT_CURRENT, FACT_CURRENT])
        self.assertEqual(extract.purposes(), ["extract", "extract"])


class RecallTest(unittest.TestCase):
    def test_recall_returns_context_without_answer_model(self):
        memory = ScriptedChat({"extract": facts_json(fact("User's dog is named Max.", [1])),
                               "query": json.dumps({"queries": ["dog name"], "keywords": ["dog", "Max"]})})
        with tempfile.TemporaryDirectory() as tmp:
            system = MemorySystem(Models(chat=None, memory=memory, embedder=HashEmbedder(),
                                         embedding_name="hash-256"), settings(), trace_dir=tmp)
            session(system, "u", "2024-01-05T10:00:00", "My dog is named Max.")
            rec = system.recall("u", "What's my dog's name?", dt("2024-01-20T09:00:00"), budget=500)
            self.assertIn("Today is", rec.context)
            self.assertIn("How to use this memory", rec.context)
            self.assertIn("[F1]", rec.memory)
            self.assertEqual(rec.items[0]["key"], "F1")
            self.assertEqual(rec.trace["budget"], 500)
            self.assertEqual(rec.trace["mode"], "recall")
            self.assertTrue(os.path.exists(os.path.join(tmp, rec.trace_id + ".json")))
            self.assertNotIn("answer", memory.purposes())

    def test_open_with_plain_functions(self):
        seen = []

        def my_llm(messages):
            seen.append(messages[0]["content"][:30])
            return HeuristicChat().chat(messages, purpose="extract" if "memory writer" in messages[0]["content"]
                                        else "query").text

        def my_embed(texts):
            return HashEmbedder().embed(texts)

        system = MemorySystem.open(None, llm=my_llm, embed=my_embed, embedding_name="my-embed-v1",
                                   discover=False, settings=settings())
        self.assertEqual(system.models.tier, "L2")
        self.assertEqual(system.models.sources["memory"], "caller")
        self.assertEqual(system.models.embedding_name, "my-embed-v1")
        session(system, "u", "2024-01-05T10:00:00", "I bought a red Honda Civic yesterday.")
        self.assertTrue(system.store("u").facts())
        rec = system.recall("u", "Which Honda did I buy?", dt("2024-01-20T09:00:00"))
        self.assertIn("Honda", rec.memory)
        self.assertTrue(seen)

    def test_open_without_anything_degrades(self):
        system = MemorySystem.open(None, discover=False, settings=settings())
        self.assertEqual(system.models.tier, "L0")
        self.assertIsNone(system.models.chat)
        self.assertFalse(system.models.semantic_embedding)


class ConcurrencyTest(unittest.TestCase):
    def test_one_store_used_from_many_threads(self):
        """回归：MCP 并发的工具调用共用一个连接时，Python 3.9 的 sqlite3 曾经直接段错误。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = MemorySystem(l0_models(), settings(), data_dir=tmp)
            session(system, "u", "2024-01-05T10:00:00", "I keep bees on my roof in Oslo.")
            handle = system.open_session("u", dt("2024-01-06T10:00:00"))
            errors = []

            def worker(n):
                try:
                    for i in range(30):
                        if n == 0:
                            system.add_turn(handle, "user", "note %d" % i, dt("2024-01-06T10:00:00"))
                        system.recall("u", "Where are my bees?", dt("2024-01-07T10:00:00"), save_trace=False)
                        system.stats("u")
                except Exception as exc:   # pragma: no cover - 失败时才走到
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(system.store("u").turns(handle.session_id)), 30)
            system.close()


class DiscoverTest(unittest.TestCase):
    def resolve(self, env, config=None, host=None):
        return resolve_models(config, UsageLedger(), host=host, env=dict(NO_OLLAMA, **env), probe=True)

    def test_nothing_found_means_l0_with_hash_vectors(self):
        models = self.resolve({})
        self.assertEqual(models.tier, "L0")
        self.assertEqual(models.sources, {"memory": "none", "chat": "none", "embedding": "builtin"})
        self.assertEqual(models.embedding_name, "hash-512@local:hash")

    def test_provider_keys_and_overrides(self):
        models = self.resolve({"DEEPSEEK_API_KEY": "x", "SILICONFLOW_API_KEY": "y"})
        self.assertEqual(models.names["memory"], "deepseek-chat")
        self.assertEqual(models.sources["memory"], "env:DEEPSEEK_API_KEY")
        self.assertEqual(models.names["embedding"], "BAAI/bge-m3")
        self.assertEqual(models.tier, "L2")
        models = self.resolve({"DEEPSEEK_API_KEY": "x", "MEMSYS_LLM_BASE_URL": "http://127.0.0.1:1/v1",
                               "MEMSYS_LLM_MODEL": "my-model"})
        self.assertEqual((models.names["memory"], models.sources["memory"]), ("my-model", "env:MEMSYS_LLM_*"))
        self.assertEqual(models.tier, "L1")

    def test_config_beats_host_beats_env(self):
        host_chat = ScriptedChat({})
        host = HostModels(memory=host_chat, names={"memory": "borrowed"}, source="upstream")
        models = self.resolve({"DEEPSEEK_API_KEY": "x"}, host=host)
        self.assertIs(models.memory, host_chat)
        self.assertEqual(models.sources["memory"], "upstream")
        config = AppConfig.from_dict({"models": {"local": {"model": "m", "base_url": "http://127.0.0.1:1/v1"}},
                                      "roles": {"memory": "local", "rerank": "local"}})
        models = self.resolve({}, config=config, host=host)
        self.assertEqual(models.sources["memory"], "config")
        self.assertIn("rerank", models.by_purpose)
        self.assertIs(models.chat, host_chat)       # 配置没写 chat：用宿主借给的

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValueError):
            AppConfig.from_dict({"models": {"m": {"model": "m", "base_url": "x"}}, "roles": {"memroy": "m"}})

    def test_ollama_probe_and_generated_config(self):
        class Tags(BaseHTTPRequestHandler):
            def do_GET(self):
                raw = json.dumps({"models": [{"name": "qwen2.5:7b-instruct"}, {"name": "bge-m3:latest"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Tags)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        env = {"OLLAMA_HOST": "http://127.0.0.1:%d" % httpd.server_address[1], "NO_PROXY": "127.0.0.1"}
        base, names = ollama_models(env)
        self.assertEqual(names, ["qwen2.5:7b-instruct", "bge-m3:latest"])
        models = resolve_models(None, UsageLedger(), env=env)
        self.assertEqual((models.names["memory"], models.names["embedding"]), ("qwen2.5:7b-instruct", "bge-m3:latest"))
        self.assertEqual(models.tier, "L2")
        data = config_from_discovery(discovered_configs(env))
        config = AppConfig.from_dict(data)
        self.assertEqual(config.roles["memory"], "qwen2.5:7b-instruct")
        self.assertFalse(data["memory"]["query_expansion"])

    def test_generated_config_without_anything_uses_hash_and_expansion(self):
        data = config_from_discovery(discovered_configs(NO_OLLAMA))
        config = AppConfig.from_dict(data)
        self.assertEqual(config.roles, {"embedding": "hash-embed"})
        self.assertTrue(config.memory.query_expansion)


class AdapterTest(unittest.TestCase):
    def test_function_chat_and_embedder(self):
        ledger = UsageLedger()
        chat = FunctionChat(lambda messages: "<think>hmm</think>{\"ok\": 1}", name="mine", ledger=ledger)
        self.assertEqual(chat.chat([{"role": "user", "content": "x"}], purpose="query").text, '{"ok": 1}')
        self.assertEqual(ledger.records()[0].model, "mine")
        self.assertFalse(ledger.records()[0].usage_reported)

        def broken(messages):
            raise RuntimeError("boom")
        with self.assertRaises(ModelError):
            FunctionChat(broken).chat([], purpose="query")
        with self.assertRaises(ModelError):
            FunctionEmbedder(lambda texts: [[1.0]]).embed(["a", "b"])

    def test_query_expansion_is_added_only_when_enabled(self):
        planner = ScriptedChat({"query": json.dumps({"queries": ["q"], "keywords": ["dog", "puppy"]})})
        models = Models(chat=None, memory=planner, embedder=HashEmbedder(), embedding_name="hash-256")
        system = MemorySystem(models, settings(query_expansion=True))
        system.recall("u", "my dog?", dt("2024-01-01T00:00:00"))
        self.assertIn("synonyms", planner.calls[-1][1][-1]["content"])
        system.settings = settings()
        system.recall("u", "my dog?", dt("2024-01-01T00:00:00"))
        self.assertNotIn("synonyms", planner.calls[-1][1][-1]["content"])


class FakeAnthropicSDK:
    def __init__(self, reply=None, error=None):
        self.calls = []
        self.reply, self.error = reply, error
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.reply


class AnthropicClientTest(unittest.TestCase):
    def reply(self, text="{}", stop="end_turn"):
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=50),
                               stop_reason=stop)

    def test_request_shape_and_usage(self):
        sdk = FakeAnthropicSDK(self.reply('{"facts": []}'))
        config = ModelConfig(model="claude-opus-5-5", base_url="https://api.anthropic.com", api="anthropic")
        ledger = UsageLedger()
        result = AnthropicClient(config, ledger, api_key="k", client=sdk).chat(
            [{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}],
            purpose="extract", max_tokens=600)
        call = sdk.calls[0]
        self.assertEqual(call["system"], "SYS")
        self.assertEqual(call["messages"], [{"role": "user", "content": "U"}])
        self.assertGreater(call["max_tokens"], 600)                  # 思考的余量
        self.assertEqual(call["extra_body"]["output_config"], {"effort": "low"})
        self.assertEqual(call["extra_body"]["fallbacks"], "default")
        self.assertEqual(result.text, '{"facts": []}')
        self.assertEqual((result.prompt_tokens, result.cached_tokens, result.completion_tokens), (150, 50, 20))
        self.assertEqual(ledger.records()[0].purpose, "extract")

    def test_truncation_and_errors(self):
        config = ModelConfig(model="claude-haiku-5-5", base_url="https://api.anthropic.com", api="anthropic")
        sdk = FakeAnthropicSDK(self.reply("{", stop="max_tokens"))
        result = AnthropicClient(config, client=sdk).chat([{"role": "user", "content": "U"}], purpose="extract")
        self.assertEqual(result.finish_reason, "length")
        self.assertNotIn("fallbacks", sdk.calls[0]["extra_body"])
        error = RuntimeError("credit balance is too low")
        error.status_code = 402
        sdk = FakeAnthropicSDK(error=error)
        with self.assertRaises(ModelError) as ctx:
            AnthropicClient(config, client=sdk).chat([{"role": "user", "content": "U"}], purpose="extract")
        self.assertTrue(ctx.exception.account_problem)

    def test_bad_api_name_is_rejected(self):
        with self.assertRaises(ValueError):
            ModelConfig.from_dict({"model": "m", "base_url": "x", "api": "gemini"})


if __name__ == "__main__":
    unittest.main()
