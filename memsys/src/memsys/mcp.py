"""MCP server：memsys mcp（标准输入输出上的 JSON-RPC）。Claude Code、Claude Desktop、Cursor 等客户端挂上就能用。

工具：memory_recall、memory_save、memory_end_session、memory_forget、memory_history、memory_status。

记忆模型按 discover 的顺序找：配置 → 客户端的模型 → 环境变量 → 本机 Ollama → 降级。
"客户端的模型"走 MCP 的 sampling：客户端在 initialize 里声明支持 sampling 时，抽取、裁决这些活
请客户端用它自己的模型代跑；不支持（或加了 --no-sampling）就往下找。

写入不要指望模型自己记得调 memory_save：Claude Code 里配上 memsys hook（见 hook.py），
由宿主每轮自动记录、自动注入；MCP 工具留给模型主动查记忆、遗忘和查看版本链。
日志只写 stderr，stdout 是协议通道。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO

from .core import MemorySystem, SessionHandle
from .llm import ChatModel, ChatResult, Message, ModelError

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "memsys", "version": "0.2.0"}

TOOLS = [
    {"name": "memory_recall",
     "description": "Search the long-term memory of past conversations with this user. Returns the relevant "
                    "remembered facts and conversation excerpts with dates, plus rules for using them. Call it "
                    "whenever the user refers to something from earlier conversations or asks about themselves.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "What to look for, usually the user's question."},
         "budget": {"type": "integer", "description": "Optional token budget for the returned memory."}},
         "required": ["query"]}},
    {"name": "memory_save",
     "description": "Record conversation turns into long-term memory. Not needed when the host already records "
                    "turns automatically (memsys hook).",
     "inputSchema": {"type": "object", "properties": {"turns": {"type": "array", "items": {
         "type": "object", "properties": {"role": {"type": "string", "enum": ["user", "assistant"]},
                                          "content": {"type": "string"}},
         "required": ["role", "content"]}}}, "required": ["turns"]}},
    {"name": "memory_end_session",
     "description": "End the current conversation session now and consolidate it into long-term memory.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memory_forget",
     "description": "Forget one memory entry by id (F12 for a fact, E5 for a conversation excerpt) when the "
                    "user asks to forget it. Entries that depended only on it are forgotten too.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "memory_history",
     "description": "Show how a remembered fact changed over time (its version chain), by fact id such as F12.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "memory_status",
     "description": "Show which memory capabilities are active and how much is stored.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class SamplingChat:
    """满足 ChatModel 协议：通过 MCP sampling 请客户端用它的模型回答。"""

    name = "mcp-sampling"

    def __init__(self, server: "McpServer", timeout_s: float = 180.0):
        self.server = server
        self.timeout_s = timeout_s

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        params: Dict[str, Any] = {
            "messages": [{"role": m["role"], "content": {"type": "text", "text": m["content"]}}
                         for m in messages if m["role"] != "system"],
            "maxTokens": max_tokens or 2000, "temperature": temperature, "includeContext": "none"}
        if system:
            params["systemPrompt"] = system
        t0 = time.monotonic()
        reply = self.server.request("sampling/createMessage", params, self.timeout_s)
        if "error" in reply:
            raise ModelError("sampling failed: %s" % json.dumps(reply["error"])[:300])
        result = reply.get("result") or {}
        content = result.get("content")
        parts = content if isinstance(content, list) else [content]
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
        finish = "length" if result.get("stopReason") == "maxTokens" else "stop"
        return ChatResult(text=text, model=str(result.get("model") or self.name), prompt_tokens=0,
                          completion_tokens=0, cached_tokens=0, cost=0.0, latency_s=time.monotonic() - t0,
                          finish_reason=finish)


class McpServer:
    def __init__(self, build_system: Callable[[Optional[ChatModel]], MemorySystem], user: str,
                 clock: Callable[[], datetime], reader: TextIO = sys.stdin, writer: TextIO = sys.stdout,
                 log: Callable[[str], None] = lambda m: print(m, file=sys.stderr), use_sampling: bool = True):
        """build_system(sampling_model) 在 initialize 时调用：客户端支持 sampling 时传入借来的模型，否则传 None。"""
        self.build_system = build_system
        self.user = user
        self.clock = clock
        self.reader, self.writer = reader, writer
        self.log = log
        self.use_sampling = use_sampling
        self.system: Optional[MemorySystem] = None
        self.session: Optional[SessionHandle] = None
        self._recovered = False
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending: Dict[int, List[Any]] = {}
        self._next_id = 0
        self._pool = ThreadPoolExecutor(max_workers=4)

    # -------------------------------------------------------------- transport
    def _send(self, message: Mapping[str, Any]) -> None:
        with self._write_lock:
            self.writer.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.writer.flush()

    def request(self, method: str, params: Mapping[str, Any], timeout_s: float) -> Dict[str, Any]:
        """向客户端发请求并等回复（sampling 用）。"""
        event = threading.Event()
        with self._write_lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = [event, None]
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not event.wait(timeout_s):
            self._pending.pop(request_id, None)
            return {"error": {"message": "timed out after %.0fs" % timeout_s}}
        return self._pending.pop(request_id)[1]

    def serve(self) -> None:
        try:
            for line in self.reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self._send({"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32700, "message": "parse error"}})
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    if message["method"] == "initialize" or "id" not in message:
                        self._dispatch(message)       # initialize 要在别的请求之前做完；通知不用回复
                    else:
                        self._pool.submit(self._dispatch, message)
                elif "id" in message and message["id"] in self._pending:
                    slot = self._pending[message["id"]]
                    slot[1] = message
                    slot[0].set()
        finally:
            for slot in list(self._pending.values()):    # 客户端没了：还在等 sampling 回复的调用直接失败
                slot[1] = {"error": {"message": "client disconnected"}}
                slot[0].set()
            self._pool.shutdown(wait=True)
            self.close()

    # -------------------------------------------------------------- dispatch
    def _dispatch(self, message: Mapping[str, Any]) -> None:
        method, request_id = message.get("method"), message.get("id")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result: Any = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call(params.get("name"), params.get("arguments") or {})
            elif request_id is None:
                return                                # notifications/initialized 等
            else:
                self._send({"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32601, "message": "method not found: %s" % method}})
                return
        except Exception as exc:
            if request_id is not None:
                self._send({"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32603, "message": "%s: %s" % (type(exc).__name__, exc)}})
            return
        if request_id is not None:
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _initialize(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[1]
        can_sample = self.use_sampling and "sampling" in (params.get("capabilities") or {})
        self.system = self.build_system(SamplingChat(self) if can_sample else None)
        models = self.system.models
        self.log("memsys mcp：用户 %s，档位 %s，记忆模型来源 %s" % (
            self.user, models.tier, models.sources.get("memory", "none")))
        return {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO,
                "instructions": "Long-term memory of past conversations with this user. Use memory_recall "
                                "when the user refers to earlier conversations or asks about themselves."}

    # -------------------------------------------------------------- tools
    def _call(self, name: Any, args: Mapping[str, Any]) -> Dict[str, Any]:
        if self.system is None:
            raise RuntimeError("not initialized")
        self._recover_once()
        try:
            text = self._tool(str(name), args)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except (ModelError, ValueError, KeyError) as exc:
            return {"content": [{"type": "text", "text": "memsys error: %s" % exc}], "isError": True}

    def _tool(self, name: str, args: Mapping[str, Any]) -> str:
        system, now = self.system, self.clock()
        if name == "memory_recall":
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("query is required")
            budget = args.get("budget")
            rec = system.recall(self.user, query, now, budget=int(budget) if budget else None)
            return rec.context + ("\n\n(trace: %s)" % rec.trace_id if rec.trace_id else "")
        if name == "memory_save":
            n = 0
            with self._state_lock:
                for turn in args.get("turns") or []:
                    role, content = turn.get("role"), str(turn.get("content") or "")
                    if role in ("user", "assistant") and content.strip():
                        if self.session is None:
                            self.session = system.open_session(self.user, now)
                        system.add_turn(self.session, role, content, now)
                        n += 1
            return "recorded %d turns" % n
        if name == "memory_end_session":
            with self._state_lock:
                if self.session is None:
                    return "no open session"
                report, lifecycle = system.close_session(self.session, now)
                self.session = None
            return (report.summary() + " " + lifecycle.summary()).strip()
        if name == "memory_forget":
            keys = system.forget(self.user, str(args.get("key") or ""))
            return "forgot " + ", ".join(keys)
        if name == "memory_history":
            chain = system.history(self.user, str(args.get("key") or ""))
            return "\n".join("[%s] %s · %s%s" % (f.key, f.when().label(), f.text,
                                                 " (replaced by F%d)" % f.superseded_by if f.superseded_by else "")
                             for f in chain)
        if name == "memory_status":
            stats = system.stats(self.user)
            models = system.models
            return json.dumps({"tier": models.tier, "models": models.names, "sources": models.sources,
                               "counts": stats["counts"], "backlog": stats["backlog"]}, ensure_ascii=False)
        raise ValueError("unknown tool %s" % name)

    def _recover_once(self) -> None:
        """第一次调用工具时补整理上次没正常结束的会话（要等 initialized 之后 sampling 才能用）。"""
        with self._state_lock:
            if self._recovered:
                return
            self._recovered = True
            try:
                for report in self.system.recover(self.user):
                    self.log("补整理了上次的会话 %s：%s" % (report.session_id, report.summary()))
            except Exception as exc:
                self.log("补整理上次的会话失败，下次再试：%s" % exc)

    def close(self) -> None:
        """客户端断开：结束当前会话。整理失败（例如 sampling 已经用不了）也没关系，下次启动会补。"""
        if self.system is None:
            return
        with self._state_lock:
            if self.session is not None:
                try:
                    self.system.close_session(self.session, self.clock())
                except Exception as exc:
                    self.log("结束会话时整理失败，原文已保存，下次启动会补：%s" % exc)
                self.session = None
        self.system.close()
