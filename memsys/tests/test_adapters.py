"""接入层：代理（OpenAI / Anthropic 两种接口）、MCP server、Claude Code hook、新命令、评测里只换 embedding。

全部对着本地的假服务跑，不联网。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memsys import prompts
from memsys.bench.longmemeval import load
from memsys.bench.runner import Runner
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, HeuristicChat
from memsys.hook import PREFIX, handle, last_assistant_text
from memsys.llm import ModelError, UsageLedger
from memsys.mcp import McpServer
from memsys.serve import Borrowed, BorrowedChat, MemoryProxy, MemoryService

from helpers import isolated_env, settings

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mini_oracle.json")
PURPOSE_BY_SYSTEM = {prompts.EXTRACT_SYSTEM: "extract", prompts.RESOLVE_SYSTEM: "resolve",
                     prompts.QUERY_SYSTEM: "query", prompts.RERANK_SYSTEM: "rerank"}
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


class Clock:
    def __init__(self, start: str):
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now


def memory_reply(messages):
    """记忆环节（借来的模型）的回复：按系统提示词认出是哪一环，交给 HeuristicChat。"""
    content = messages[0].get("content") if messages else None
    purpose = PURPOSE_BY_SYSTEM.get(content) if isinstance(content, str) else None
    return HeuristicChat().chat(messages, purpose=purpose).text if purpose else None


class FakeUpstream:
    """假的上游：OpenAI 的 /v1/chat/completions 和 Anthropic 的 /v1/messages，支持流式。"""

    def __init__(self):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if self.path.endswith("/chat/completions"):
                    self.openai(body)
                elif self.path.endswith("/messages"):
                    self.anthropic(body)
                else:
                    self.send_json(404, {"error": "nope"})

            def openai(self, body):
                messages = body["messages"]
                memory = memory_reply(messages)
                if memory is not None:
                    return self.send_json(200, {"choices": [{"message": {"role": "assistant", "content": memory},
                                                             "finish_reason": "stop"}]})
                last = messages[-1]
                # 只看用户这句话本身：注入的记忆里可能也出现过 CALL_TOOL
                if last["role"] == "user" and str(last["content"]).rstrip().endswith("CALL_TOOL please"):
                    return self.send_json(200, {"choices": [{"message": {"role": "assistant", "content": None,
                                                                         "tool_calls": [{"id": "t1"}]},
                                                             "finish_reason": "tool_calls"}]})
                if body.get("stream"):
                    events = [{"choices": [{"delta": {"content": "Sure, "}, "finish_reason": None}]},
                              {"choices": [{"delta": {"content": "noted."}, "finish_reason": None}]},
                              {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
                    return self.send_stream(["data: %s" % json.dumps(e) for e in events] + ["data: [DONE]"])
                self.send_json(200, {"choices": [{"message": {"role": "assistant", "content": "Noted."},
                                                  "finish_reason": "stop"}]})

            def anthropic(self, body):
                if body.get("stream"):
                    events = [{"type": "message_start", "message": {}},
                              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Got "}},
                              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "it."}},
                              {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                              {"type": "message_stop"}]
                    lines = []
                    for e in events:
                        lines += ["event: %s" % e["type"], "data: %s" % json.dumps(e), ""]
                    return self.send_stream(lines)
                self.send_json(200, {"content": [{"type": "text", "text": "Got it."}], "stop_reason": "end_turn"})

            def send_json(self, status, obj):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def send_stream(self, lines):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for line in lines:
                    self.wfile.write((line + "\n").encode())
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def agent_requests(self):
        return [r for r in self.requests if memory_reply(r[2].get("messages") or [{}]) is None]

    def memory_requests(self):
        return [r for r in self.requests if memory_reply(r[2].get("messages") or [{}]) is not None]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def post(url, body, headers=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    with urllib.request.urlopen(request, timeout=30) as resp:
        return resp.status, dict(resp.headers), resp.read()


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.upstream = FakeUpstream()
        self.addCleanup(self.upstream.close)
        self.clock = Clock("2024-05-01T09:00:00")
        self.logs = []

    def start(self, upstream_path="/v1", borrow=True):
        upstream = self.upstream.url + upstream_path
        ledger = UsageLedger()
        self.borrowed = Borrowed(upstream) if borrow else None
        memory = BorrowedChat(self.borrowed, ledger) if borrow else HeuristicChat()
        models = Models(chat=None, memory=memory, embedder=HashEmbedder(), embedding_name="hash-256",
                        semantic_embedding=False)
        self.service = MemoryService(models, settings(), self.tmp.name, clock=self.clock, idle_minutes=30,
                                     log=self.logs.append, tick_s=3600)
        self.server = MemoryProxy(("127.0.0.1", 0), self.service, upstream, self.borrowed, default_user="anon",
                                  log=self.logs.append)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.service.shutdown, 30)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def turns(self, user):
        store = self.service.req.store(user)
        return [(t["role"], t["content"]) for sid in [r for r in self._sessions(store)]
                for t in store.turns(sid)]

    @staticmethod
    def _sessions(store):
        return [r["id"] for r in store.db.execute("SELECT id FROM sessions ORDER BY opened_at, id")]

    def test_openai_round_trip_tool_loop_borrowing_and_stream(self):
        self.start()
        auth = {"Authorization": "Bearer agent-key", "X-Memsys-User": "alice"}
        history = [{"role": "system", "content": "You are helpful."},
                   {"role": "user", "content": "I adopted a beagle named Max."}]
        status, headers, raw = post(self.base + "/v1/chat/completions",
                                    {"model": "agent-model", "messages": history}, auth)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "Noted.")
        self.assertIn("X-Memsys-Trace", headers)
        sent = self.upstream.agent_requests()[-1]
        self.assertEqual(sent[2]["messages"][0], history[0])                   # 系统提示词原样不动
        self.assertTrue(sent[2]["messages"][1]["content"].startswith("<memory>"))
        self.assertTrue(sent[2]["messages"][1]["content"].endswith("I adopted a beagle named Max."))
        self.assertNotIn("x-memsys-user", sent[1])
        self.assertEqual(self.turns("alice"), [("user", "I adopted a beagle named Max."), ("assistant", "Noted.")])

        # 工具调用循环：同一轮重发，不重复记、不重复检索；中间那次 tool_calls 不记
        memory_calls = len(self.upstream.memory_requests())
        loop = history + [{"role": "assistant", "content": "Noted."},
                          {"role": "user", "content": "CALL_TOOL please"}]
        post(self.base + "/v1/chat/completions", {"model": "agent-model", "messages": loop}, auth)
        loop2 = loop + [{"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
                        {"role": "tool", "tool_call_id": "t1", "content": "done"}]
        post(self.base + "/v1/chat/completions", {"model": "agent-model", "messages": loop2}, auth)
        self.assertEqual(self.turns("alice")[2:], [("user", "CALL_TOOL please"), ("assistant", "Noted.")])
        self.assertEqual(len(self.upstream.memory_requests()) - memory_calls, 1)   # 只有新一轮的查询分析

        # 结束会话：后台借上游的模型和 agent 的 key 整理
        post(self.base + "/memsys/end", {"user": "alice"})
        self.assertTrue(self.service.drain(30))
        extract = [r for r in self.upstream.memory_requests()
                   if r[2]["messages"][0]["content"] == prompts.EXTRACT_SYSTEM]
        self.assertTrue(extract)
        self.assertEqual(extract[0][1]["authorization"], "Bearer agent-key")
        self.assertEqual(extract[0][2]["model"], "agent-model")
        facts = self.service.req.store("alice").facts()
        self.assertTrue(any("Max" in f.text for f in facts))

        # 下一个会话：流式；记忆里带上了 Max
        self.clock.now += timedelta(days=2)
        request = urllib.request.Request(self.base + "/v1/chat/completions", method="POST", data=json.dumps(
            {"model": "agent-model", "stream": True,
             "messages": [{"role": "user", "content": "What is my beagle called?"}]}).encode(),
            headers=dict(auth, **{"Content-Type": "application/json"}))
        with urllib.request.urlopen(request, timeout=30) as resp:
            streamed = resp.read().decode()
        self.assertIn("noted.", streamed)
        self.assertIn("[DONE]", streamed)
        injected = self.upstream.agent_requests()[-1][2]["messages"][-1]["content"]
        self.assertIn("Max", injected.split("</memory>")[0])
        self.assertEqual(self.turns("alice")[-1], ("assistant", "Sure, noted."))

        status = json.loads(urllib.request.urlopen(self.base + "/memsys/status", timeout=10).read())
        self.assertIn("alice", status["users"])
        self.assertEqual((status["tier"], status["queued"]), ("L1", 0))

    def test_memory_off_failures_and_idle_sessions(self):
        self.start(borrow=False)
        body = {"model": "m", "messages": [{"role": "user", "content": "Plain question."}]}
        post(self.base + "/v1/chat/completions", body, {"X-Memsys-Memory": "off"})
        self.assertEqual(self.upstream.agent_requests()[-1][2]["messages"][0]["content"], "Plain question.")
        self.assertEqual(self.service.req.store("anon").counts()["session"], {})

        def broken(*args, **kwargs):
            raise RuntimeError("memory is down")
        original = self.service.recall
        self.service.recall = broken
        status, _, raw = post(self.base + "/v1/chat/completions", body)
        self.assertEqual(status, 200)                          # 记忆坏了，agent 照常拿到回答
        self.assertEqual(self.upstream.agent_requests()[-1][2]["messages"][0]["content"], "Plain question.")
        self.assertTrue(any("没有带上记忆" in line for line in self.logs))
        self.service.recall = original

        self.clock.now += timedelta(minutes=45)              # 空闲超时：下一句开新会话，旧的交给后台整理
        post(self.base + "/v1/chat/completions",
             {"model": "m", "messages": [{"role": "user", "content": "Another day, another question."}]})
        self.assertTrue(self.service.drain(30))
        sessions = self.service.req.store("anon").counts()["session"]
        self.assertEqual(sessions, {"closed": 1, "open": 1})

    def test_anthropic_messages_and_stream(self):
        self.start(upstream_path="", borrow=False)
        headers = {"x-api-key": "ak", "anthropic-version": "2023-06-01"}
        body = {"model": "claude-x", "max_tokens": 100, "system": [{"type": "text", "text": "SYS"}],
                "metadata": {"user_id": "carol"},
                "messages": [{"role": "user", "content": [{"type": "text", "text": "My sister Ann lives in Paris."}]}]}
        status, _, raw = post(self.base + "/v1/messages", body, headers)
        self.assertEqual(json.loads(raw)["content"][0]["text"], "Got it.")
        path, sent_headers, sent = self.upstream.agent_requests()[-1]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(sent_headers["x-api-key"], "ak")
        self.assertEqual(sent["system"], body["system"])
        blocks = sent["messages"][0]["content"]
        self.assertTrue(blocks[0]["text"].startswith("<memory>"))
        self.assertEqual(blocks[1]["text"], "My sister Ann lives in Paris.")

        loop = dict(body, stream=True, messages=body["messages"] + [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}])
        request = urllib.request.Request(self.base + "/v1/messages", method="POST", data=json.dumps(loop).encode(),
                                         headers=dict(headers, **{"Content-Type": "application/json"}))
        with urllib.request.urlopen(request, timeout=30) as resp:
            self.assertIn("message_stop", resp.read().decode())
        sent = self.upstream.agent_requests()[-1][2]
        self.assertTrue(sent["messages"][0]["content"][0]["text"].startswith("<memory>"))
        self.assertEqual(sent["messages"][2], loop["messages"][2])     # tool_result 不动
        self.assertEqual(self.turns("carol"), [("user", "My sister Ann lives in Paris."),
                                               ("assistant", "Got it.")])   # 重复的回答不记两遍

    def test_rest_endpoints(self):
        self.start(borrow=False)
        _, _, raw = post(self.base + "/memsys/turns", {"user": "dave", "turns": [
            {"role": "user", "content": "I run 5k every Sunday morning."}, {"role": "assistant", "content": "Nice!"},
            {"role": "system", "content": "ignored"}]})
        self.assertEqual(json.loads(raw), {"recorded": 2})
        post(self.base + "/memsys/end", {"user": "dave"})
        self.assertTrue(self.service.drain(30))
        _, _, raw = post(self.base + "/memsys/recall", {"user": "dave", "query": "When do I run 5k?", "budget": 800})
        data = json.loads(raw)
        self.assertIn("Sunday", data["memory"])
        self.assertEqual(data["tier"], "L1")
        self.assertTrue(data["items"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            post(self.base + "/memsys/forget", {"user": "dave", "key": "nonsense"})
        self.assertEqual(ctx.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            post(self.base + "/memsys/recall", {"user": "dave"})
        self.assertEqual(ctx.exception.code, 400)


class McpTest(unittest.TestCase):
    def test_tools_with_sampling(self):
        inbox: "queue.Queue" = queue.Queue()
        replies, done = {}, {}

        class Writer:
            def write(self, text):
                for line in text.splitlines():
                    message = json.loads(line)
                    if message.get("method") == "sampling/createMessage":
                        params = message["params"]
                        messages = [{"role": "system", "content": params.get("systemPrompt", "")}] + [
                            {"role": m["role"], "content": m["content"]["text"]} for m in params["messages"]]
                        text = memory_reply(messages) or "{}"
                        inbox.put(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                            "role": "assistant", "content": {"type": "text", "text": text}, "model": "client-model",
                            "stopReason": "endTurn"}}))
                    elif "id" in message:
                        replies[message["id"]] = message
                        done.setdefault(message["id"], threading.Event()).set()

            def flush(self):
                pass

        def reader():
            while True:
                line = inbox.get()
                if line is None:
                    return
                yield line + "\n"

        def build(sampling):
            self.assertIsNotNone(sampling)
            models = Models(chat=None, memory=sampling, embedder=HashEmbedder(), embedding_name="hash-256",
                            semantic_embedding=False)
            return MemorySystem(models, settings())

        clock = Clock("2024-06-01T10:00:00")
        server = McpServer(build, "erin", clock, reader=reader(), writer=Writer(), log=lambda m: None)
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()

        def call(request_id, method, params=None):
            done.setdefault(request_id, threading.Event())
            inbox.put(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}))
            self.assertTrue(done[request_id].wait(30))
            return replies[request_id]

        init = call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {"sampling": {}},
                                      "clientInfo": {"name": "test"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        inbox.put(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = call(2, "tools/list")["result"]["tools"]
        self.assertIn("memory_recall", [t["name"] for t in tools])
        saved = call(3, "tools/call", {"name": "memory_save", "arguments": {"turns": [
            {"role": "user", "content": "My favourite tea is genmaicha."}, {"role": "assistant", "content": "Lovely."}]}})
        self.assertEqual(saved["result"]["content"][0]["text"], "recorded 2 turns")
        ended = call(4, "tools/call", {"name": "memory_end_session", "arguments": {}})
        self.assertIn("1 条新事实", ended["result"]["content"][0]["text"])
        clock.now += timedelta(days=3)
        recalled = call(5, "tools/call", {"name": "memory_recall", "arguments": {"query": "Which tea do I like?"}})
        self.assertIn("genmaicha", recalled["result"]["content"][0]["text"])
        status = json.loads(call(6, "tools/call", {"name": "memory_status", "arguments": {}})["result"]["content"][0]["text"])
        self.assertEqual((status["tier"], status["sources"].get("memory")), ("L1", None))
        bad = call(7, "tools/call", {"name": "memory_forget", "arguments": {"key": "zz"}})
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("error", call(8, "no/such/method"))
        inbox.put(None)
        thread.join(10)
        self.assertFalse(thread.is_alive())


class HookTest(unittest.TestCase):
    def test_claude_code_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Models(chat=None, memory=HeuristicChat(), embedder=HashEmbedder(), embedding_name="hash-256",
                            semantic_embedding=False)
            system = MemorySystem(models, settings(), data_dir=tmp)
            now = datetime(2024, 7, 1, 9, 0)
            out = handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                          "prompt": "I started learning the cello in March."}, system, "me", now)
            self.assertIn("Long-term memory", out)
            transcript = os.path.join(tmp, "t.jsonl")
            with open(transcript, "w") as fh:
                for entry in [
                        {"type": "user", "message": {"role": "user", "content": "I started learning the cello in March."}},
                        {"type": "assistant", "message": {"role": "assistant", "content": [
                            {"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "x"}]}},
                        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "r"}]}},
                        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<meta>"}},
                        {"type": "assistant", "message": {"role": "assistant", "content": [
                            {"type": "text", "text": "Great choice!"}]}}]:
                    fh.write(json.dumps(entry) + "\n")
            self.assertEqual(last_assistant_text(transcript), "Let me look.\n\nGreat choice!")
            stop = {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": transcript}
            handle(stop, system, "me", now)
            handle(stop, system, "me", now)                       # 同一个回答不记两遍
            store = system.store("me")
            session_id = store.find_open_session(PREFIX + "s1")
            self.assertEqual([t["role"] for t in store.turns(session_id)], ["user", "assistant"])

            # Claude Code 被直接关掉，下次启动时补整理
            handle({"hook_event_name": "SessionStart", "session_id": "s2"}, system, "me", now + timedelta(days=1))
            self.assertIsNone(store.find_open_session(PREFIX + "s1"))
            self.assertTrue(any("cello" in f.text for f in store.facts()))

            handle({"hook_event_name": "UserPromptSubmit", "session_id": "s2", "prompt": "When did I start the cello?"},
                   system, "me", now + timedelta(days=1))
            handle({"hook_event_name": "SessionEnd", "session_id": "s2"}, system, "me", now + timedelta(days=1))
            self.assertEqual(store.open_sessions(), [])
            system.close()


class CommandTest(unittest.TestCase):
    err = io.StringIO()

    def run_cli(self, *argv, stdin=None):
        from memsys.cli import main
        out = io.StringIO()
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            saved_stdin = sys.stdin
            try:
                if stdin is not None:
                    sys.stdin = io.StringIO(stdin)
                with isolated_env(os.path.join(tmp, "home")), contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(self.err):
                    code = main(list(argv))
                files = {}
                for name in os.listdir(tmp):
                    if name.endswith(".json"):
                        with open(name) as fh:
                            files[name] = fh.read()
            finally:
                sys.stdin = saved_stdin
                os.chdir(cwd)
        return code, out.getvalue(), files

    def test_doctor_init_rebuild_and_hook(self):
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("档位：L0", out)
        self.assertIn("没有（全部自动查找）", out)
        code, out, files = self.run_cli("init")
        self.assertIn("config.json", files)
        self.assertEqual(json.loads(files["config.json"])["roles"], {"embedding": "hash-embed"})
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("rebuild", "--user", "u")
        self.assertIn("没有记忆模型", str(ctx.exception))
        code, out, _ = self.run_cli("hook", stdin="not json")
        self.assertEqual((code, out), (0, ""))          # 出错只往 stderr 写一句，不挡住 Claude Code
        self.assertIn("memsys hook", self.err.getvalue())

    def test_doctor_check_returns_failure_for_unavailable_model(self):
        class Unavailable(HeuristicChat):
            def chat(self, *args, **kwargs):
                raise ModelError("self-authored test: unavailable model")
        model = Unavailable()
        models = Models(chat=model, memory=model, embedder=HashEmbedder(), embedding_name="hash")
        def load_config(args, ledger):
            args.config_path = None
            return None, models, settings()
        with patch("memsys.cli._load", side_effect=load_config):
            code, out, _ = self.run_cli("doctor", "--check")
        self.assertEqual(code, 1)
        self.assertIn("失败", out)


class BenchReindexTest(unittest.TestCase):
    def test_embedding_change_reingests_for_end_to_end_update_consistency(self):
        class Counting(HeuristicChat):
            calls = []

            def chat(self, messages, *, purpose, **kwargs):
                Counting.calls.append(purpose)
                return super().chat(messages, purpose=purpose, **kwargs)

        items = [item for item, _ in load(FIXTURE)][:1]
        with tempfile.TemporaryDirectory() as tmp:
            first = Models(chat=Counting(), memory=Counting(), embedder=HashEmbedder(dim=256),
                           embedding_name="hash-256", names={"chat": "fake", "memory": "fake"})
            Runner(first, settings(), tmp).run(items)
            self.assertIn("extract", Counting.calls)
            os.remove(os.path.join(tmp, "hypotheses.jsonl"))
            Counting.calls.clear()
            second = Models(chat=Counting(), memory=Counting(), embedder=HashEmbedder(dim=128),
                            embedding_name="hash-128", names={"chat": "fake", "memory": "fake"})
            Runner(second, settings(), tmp, allow_config_change=True).run(items)
            self.assertIn("extract", Counting.calls)
            with open(os.path.join(tmp, "timings.jsonl")) as fh:
                timing = [json.loads(line) for line in fh][-1]
            self.assertFalse(timing["reindexed"])
            self.assertFalse(timing["cached_ingest"])


if __name__ == "__main__":
    unittest.main()
