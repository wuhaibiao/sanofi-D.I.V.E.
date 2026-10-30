"""代理与 HTTP 接口：memsys serve。

说 OpenAI 或 Anthropic 接口的 agent，把接口地址指向这里就接上了记忆，agent 自己一行代码不用改：

    agent ──▶ memsys serve ──▶ 原来的上游模型
              │ 每轮：取最后一条用户消息 → recall → 记忆块插在这条消息前面 → 转发
              │ 回复：原样转回（支持流式），同时记下这一轮的用户消息和最终回答
              └ 会话：空闲超时或调用 /memsys/end 结束，结束后在后台抽取与保存记忆

同一个端口还提供不经过模型的记忆接口，给不方便走代理的 agent（任何语言）直接调用：

    POST /memsys/recall  {"user", "query", "budget"?}   → {"context", "memory", "rules", "items", "trace_id", "tier"}
    POST /memsys/turns   {"user", "turns": [{"role", "content"}]}
    POST /memsys/end     {"user"}
    POST /memsys/forget  {"user", "key"}
    GET  /memsys/status

用户身份依次看请求头 X-Memsys-User、OpenAI 请求体里的 user、Anthropic 请求体里的 metadata.user_id，
都没有就用启动时的 --user。请求头 X-Memsys-Memory: off 可以让单个请求不经过记忆。

记忆模型默认借上游：同一个上游地址、agent 请求里带来的 key（只在内存里，不写盘）和模型名；
配置文件或 --memory-model 可以另外指定。Anthropic 上游只借 x-api-key，不借 OAuth 令牌。
记忆出任何错都不影响转发：这一轮不带记忆，照常回答。只监听本机地址。
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import MemorySettings, Models
from .core import MemorySystem, Recall, SessionHandle
from .llm import BadJSONError, IncompleteJSONError, RETRYABLE_STATUS, ChatResult, Message, ModelConfig, ModelError, UsageLedger
from .timeutil import from_iso
from .write import Writer
from .store import WriteConflict

DEFAULT_PORT = 8848
_HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "keep-alive",
        "proxy-authorization", "proxy-connection", "te", "upgrade"}
_RESPONSE_SKIP = {"content-length", "transfer-encoding", "connection", "content-encoding", "keep-alive"}


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _text_of(content: Any) -> str:
    """消息内容可能是字符串，也可能是内容块列表（OpenAI 的 parts、Anthropic 的 blocks）：只取文字。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content
                         if isinstance(part, dict) and part.get("type") in ("text", "input_text")
                         and part.get("text")).strip()
    return ""


def _prepend(content: Any, text: str) -> Any:
    """把记忆块放到这条用户消息的最前面。放在最后一条用户消息里而不是系统提示词里，
    agent 的系统提示词和更早的历史保持不变，服务商的前缀缓存不会因为记忆每轮不同而整段失效。"""
    if isinstance(content, list):
        return [{"type": "text", "text": text}] + list(content)
    return text + "\n\n" + (content or "")


class OpenAIChat:
    """POST …/chat/completions"""
    api = "openai"

    @staticmethod
    def user_id(body: Mapping[str, Any]) -> Optional[str]:
        return body.get("user") if isinstance(body.get("user"), str) else None

    @staticmethod
    def last_user(body: Mapping[str, Any]) -> Optional[Tuple[int, str, int]]:
        """(消息下标, 文字, 这是第几条用户消息)；工具调用循环里最后一条是 tool 消息，要往前找。"""
        messages = body.get("messages") or []
        count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if isinstance(m, dict) and m.get("role") == "user":
                text = _text_of(m.get("content"))
                return (i, text, count) if text else None
        return None

    @staticmethod
    def inject(body: Dict[str, Any], index: int, block: str) -> None:
        message = dict(body["messages"][index])
        message["content"] = _prepend(message.get("content"), block)
        body["messages"] = list(body["messages"])
        body["messages"][index] = message

    @staticmethod
    def final_text(data: Mapping[str, Any]) -> Optional[str]:
        """非流式回复里的最终回答；还在调工具（finish_reason=tool_calls）时返回 None，不记。"""
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return None
        choice = choices[0]
        if choice.get("finish_reason") not in ("stop", "length", None):
            return None
        text = _text_of((choice.get("message") or {}).get("content"))
        return text or None

    @staticmethod
    def stream_event(event: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
        """流式事件 → (增量文字, 结束原因)。"""
        choices = event.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return "", None
        delta = choices[0].get("delta") or {}
        text = delta.get("content") if isinstance(delta.get("content"), str) else ""
        return text, choices[0].get("finish_reason")

    @staticmethod
    def finished(reason: Optional[str]) -> bool:
        return reason in ("stop", "length")


class AnthropicMessages:
    """POST …/v1/messages"""
    api = "anthropic"

    @staticmethod
    def user_id(body: Mapping[str, Any]) -> Optional[str]:
        meta = body.get("metadata")
        return meta.get("user_id") if isinstance(meta, dict) and isinstance(meta.get("user_id"), str) else None

    @staticmethod
    def last_user(body: Mapping[str, Any]) -> Optional[Tuple[int, str, int]]:
        """工具调用循环里最后一条 user 消息只有 tool_result，要往前找到带文字的那条。"""
        messages = body.get("messages") or []
        count = 0
        found: Optional[Tuple[int, str]] = None
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                text = _text_of(m.get("content"))
                if text:
                    count += 1
                    found = (i, text)
        return (found[0], found[1], count) if found else None

    inject = OpenAIChat.inject

    @staticmethod
    def final_text(data: Mapping[str, Any]) -> Optional[str]:
        if data.get("stop_reason") not in ("end_turn", "max_tokens", "stop_sequence"):
            return None
        return _text_of(data.get("content")) or None

    @staticmethod
    def stream_event(event: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
        kind = event.get("type")
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            return (delta.get("text") or "") if delta.get("type") == "text_delta" else "", None
        if kind == "message_delta":
            return "", (event.get("delta") or {}).get("stop_reason")
        return "", None

    @staticmethod
    def finished(reason: Optional[str]) -> bool:
        return reason in ("end_turn", "max_tokens", "stop_sequence")


# ------------------------------------------------------------ borrowed model
class Borrowed:
    """agent 最近一次请求里带来的 key 和模型名，记忆模型借来用。只在内存里。"""

    def __init__(self, upstream: str, model: Optional[str] = None, api: Optional[str] = None):
        self.upstream = upstream.rstrip("/")
        self.fixed_model = model
        self.model = model
        self.api = api or ("anthropic" if "anthropic.com" in upstream else None)
        self.key: Optional[str] = None
        self._lock = threading.Lock()

    def update(self, headers: Mapping[str, str], model: Optional[str], api: str) -> None:
        lowered = {k.lower(): v for k, v in headers.items()}
        if api == "anthropic":
            key = lowered.get("x-api-key")   # Authorization: Bearer 在这里是 OAuth 令牌，不借
        else:
            auth = lowered.get("authorization", "")
            key = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        with self._lock:
            if key:
                self.key = key
            if model and not self.fixed_model:
                self.model = model
            self.api = api

    def snapshot(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        with self._lock:
            return self.key, self.model, self.api


class BorrowedChat:
    """满足 ChatModel 协议：每次调用时按 agent 最近用的模型和 key，去同一个上游调用。"""

    def __init__(self, borrowed: Borrowed, ledger: UsageLedger):
        self.borrowed = borrowed
        self.ledger = ledger
        self.name = "upstream:" + (borrowed.fixed_model or "agent-model")
        self._clients: Dict[Tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        _, model, api = self.borrowed.snapshot()
        if not model:
            raise ModelError("no agent request has been seen yet, so there is no model to borrow; "
                             "pass --memory-model or configure roles.memory")
        api = api or "openai"
        with self._lock:
            client = self._clients.get((api, model))
            if client is None:
                from .config import make_client
                base = self.borrowed.upstream
                if api == "openai" and not base.endswith("/v1"):
                    base += "/v1"
                client = make_client(ModelConfig(model=model, base_url=base, api=api, json_mode=False),
                                     self.ledger, api_key=lambda: self.borrowed.snapshot()[0])
                self._clients[(api, model)] = client
        return client.chat(messages, purpose=purpose, temperature=temperature, max_tokens=max_tokens,
                           json_mode=json_mode, tags=tags)


# ------------------------------------------------------------ sessions
@dataclass
class Job:
    kind: str                    # close / flush
    user: str
    session_id: str
    at: datetime
    upto: Optional[int] = None   # flush：只整理到这一句（最后一条助手回复之后）
    attempts: int = 0
    not_before: float = 0.0


@dataclass
class _UserState:
    session: Optional[SessionHandle] = None
    last_active: Optional[datetime] = None
    last_user_key: Optional[str] = None
    recall_key: Optional[str] = None
    recall: Optional[Recall] = None
    flushing: bool = False
    seen: bool = False


class MemoryService:
    """会话管理与持久后台任务；不同用户并行，同一任务以 Qdrant 条件租约协调。"""

    def __init__(self, models: Models, settings: MemorySettings, data_dir: str,
                 clock: Callable[[], datetime] = _now, idle_minutes: float = 30,
                 log: Callable[[str], None] = print, tick_s: float = 30.0, *, storage=None):
        self.req = MemorySystem(models, settings, data_dir=data_dir,
                                trace_dir=os.path.join(data_dir, "traces"), wal=True, storage=storage)
        self.bg = MemorySystem(models, settings, data_dir=data_dir, wal=True, storage=storage)
        self.clock = clock
        self.idle = timedelta(minutes=idle_minutes) if idle_minutes > 0 else None
        self.log = log
        self.tick_s = tick_s
        self._state: Dict[str, _UserState] = {}
        self._locks: Dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._jobs: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._retry: List[Job] = []
        self._busy = 0
        self._worker_id = str(uuid.uuid4())
        self._workers = [threading.Thread(target=self._work, name="memsys-consolidate-%d" % i, daemon=True)
                         for i in range(settings.worker_count)]
        for worker in self._workers:
            worker.start()

    @property
    def models(self) -> Models:
        return self.req.models

    def lock(self, user: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(user, threading.RLock())

    def _user(self, user: str) -> _UserState:
        """第一次见到这个用户时，把上次没关掉的会话交给后台整理（进程被杀、机器重启）。"""
        st = self._state.setdefault(user, _UserState())
        if not st.seen:
            st.seen = True
            store = self.req.store(user)
            # Durable tasks survive restarts; active explicit sessions are not closed on first read.
            for row in store.records("job"):
                if row["status"] != "completed":
                    self._jobs.put(Job(row["kind"], user, row["session_id"], from_iso(row["at"]), row.get("upto")))
        return st

    def _session(self, user: str, st: _UserState, now: datetime, idle_close: bool = True) -> SessionHandle:
        if idle_close and st.session is not None and self.idle and st.last_active \
                and now - st.last_active > self.idle:
            self._close(user, st)
        if st.session is not None and self.req.store(user).session(st.session.session_id)["status"] != "open":
            st.session = None
        if st.session is None:
            st.session = self.req.open_session(user, now, external_id="memsys-proxy")
            st.last_user_key = None
        st.last_active = now
        return st.session

    def _close(self, user: str, st: _UserState) -> None:
        if st.session is None:
            return
        self._enqueue(Job("close", user, st.session.session_id, st.last_active or self.clock()))
        st.session, st.recall_key, st.recall, st.last_user_key = None, None, None, None

    # -------------------------------------------------------------- API
    def record_user(self, user: str, text: str, key: str, now: datetime) -> bool:
        """记下一条用户消息。key 相同（重试、工具调用循环里重发的同一轮）就不重复记。"""
        with self.lock(user):
            st = self._user(user)
            handle = self._session(user, st, now)
            if key == st.last_user_key:
                return False
            self.req.add_turn(handle, "user", text, now, idempotency_key=key)
            st.last_user_key = key
            return True

    def record_assistant(self, user: str, text: str, now: datetime) -> None:
        with self.lock(user):
            st = self._user(user)
            # 回答属于刚才那条用户消息所在的会话：工具调用很久也不能在这里因为"空闲"把会话切开
            handle = self._session(user, st, now, idle_close=False)
            store = self.req.store(user)
            last = store.last_turn(handle.session_id)
            if last is not None and last["role"] == "assistant" and last["content"] == text:
                return
            self.req.add_turn(handle, "assistant", text, now)
            row = store.session(handle.session_id)
            pending = len(store.turns(handle.session_id, row["turns_done"]))
            if not st.flushing and pending >= 2 * self.req.settings.flush_rounds:
                # 会话很长：先把到这条回复为止的部分整理掉，免得一直不结束的会话里的事实迟迟查不到
                st.flushing = True
                upto = store.last_turn(handle.session_id)["idx"] + 1
                self._enqueue(Job("flush", user, handle.session_id, now, upto=upto))

    def add_turns(self, user: str, turns: Sequence[Mapping[str, str]], now: datetime) -> int:
        n = 0
        for turn in turns:
            role, content = turn.get("role"), str(turn.get("content") or "")
            if role not in ("user", "assistant") or not content.strip():
                continue
            if role == "user":
                key = "rest:%s" % hashlib.sha1(("%s|%s" % (now.isoformat(), content)).encode()).hexdigest()
                n += self.record_user(user, content, key, now)
            else:
                self.record_assistant(user, content, now)
                n += 1
        return n

    def recall(self, user: str, query: str, now: datetime, key: Optional[str] = None,
               budget: Optional[int] = None, coverage_budget: Optional[int] = None) -> Recall:
        """同一轮（key 相同）只检索一次：工具调用循环里 agent 会把同一轮发很多遍。"""
        # Model calls run outside the short in-process state lock.
        revision = self.req.store(user).get_meta("revision")
        cache_key = (key, query, now.date().isoformat(), revision, budget, coverage_budget)
        with self.lock(user):
            st = self._user(user)
            if key is not None and cache_key == st.recall_key and st.recall is not None:
                return st.recall
        rec = self.req.recall(user, query, now, budget=budget, coverage_budget=coverage_budget)
        with self.lock(user):
            st = self._user(user)
            if key is not None:
                st.recall_key, st.recall = cache_key, rec
        return rec

    def end(self, user: str) -> bool:
        with self.lock(user):
            st = self._user(user)
            had = st.session is not None
            self._close(user, st)
            return had

    def forget(self, user: str, key: str) -> List[str]:
        with self.lock(user):
            return self.req.forget(user, key)

    def status(self, only_user=None):
        from .store import Store
        users = {}
        names = [only_user] if only_user else sorted(set(self._state) | set(self.req._stores) |
                          set(Store.users(self.req.storage, client=self.req._qdrant_client)))
        retries, pending, failed = 0, 0, 0
        for user in names:
            store = self.req.store(user)
            jobs = store.records("job")
            sessions = store.records("session")
            retries += sum(j["status"] == "retry" for j in jobs)
            pending += sum(j["status"] in ("queued", "retry", "running") for j in jobs)
            failed += sum(j["status"] == "failed" for j in jobs)
            st = self._state.get(user)
            users[user] = dict(session=st.session.session_id if st and st.session else None,
                sessions=[dict(id=r["id"], status=r["status"], turns_done=r["turns_done"], next_idx=r["next_idx"]) for r in sessions],
                backlog=store.backlog_count(), counts=store.counts(),
                jobs=[{k: r.get(k) for k in ("key", "kind", "session_id", "status", "attempts", "not_before",
                    "queued_at", "started_at", "completed_at", "error_type", "error")} for r in jobs])
        return dict(tier=self.models.tier, models=self.models.names, sources=self.models.sources,
                    users=users, queued=pending, retrying=retries, failed=failed,
                    recall_mode=self.req.settings.recall_mode)

    def retry_job(self, user, key):
        """Only an explicit request requeues a permanently failed extraction."""
        row = self.req.store(user).record("job", key)
        if not row or row.get("status") != "failed":
            raise ValueError("job_key must identify a failed job for this user")
        self._enqueue(Job(row["kind"], user, row["session_id"], from_iso(row["at"]), row.get("upto")),
                      retry_failed=True)

    def drain(self, timeout: float = 600.0) -> bool:
        """等后台把排着的整理做完（测试和关闭时用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._jobs.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def shutdown(self, timeout: float = 600.0) -> None:
        """结束所有会话并等整理完。整理失败也没关系：原文都在库里，下次启动会补。"""
        for user in list(self._state):
            with self.lock(user):
                self._close(user, self._state[user])
        for job in self._retry:
            self._jobs.put(job)
        self._retry = []
        self.drain(timeout)
        for worker in self._workers:
            self._jobs.put(None)
        for worker in self._workers:
            worker.join(timeout=5)
        self.req.close()
        self.bg.close()

    # -------------------------------------------------------------- worker
    def _work(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=self.tick_s)
            except queue.Empty:
                try:
                    self._tick()
                except Exception as exc:
                    self.log("后台任务恢复暂不可用：%s" % str(exc)[:150])
                continue
            if job is None:
                self._jobs.task_done()
                return
            try:
                with self._guard:
                    self._busy += 1
                self._run(job)
            finally:
                with self._guard:
                    self._busy -= 1
                self._jobs.task_done()

    @staticmethod
    def _job_key(job):
        return "%s:%s:%s" % (job.kind, job.session_id, job.upto)

    def _enqueue(self, job, *, retry_failed=False):
        store = self.req.store(job.user)
        key = self._job_key(job)
        if job.kind == "close":
            store.request_close(job.session_id, job.at)
        with store.transaction():
            previous = store.record("job", key)
            if previous and previous.get("status") == "completed":
                return
            if previous and previous.get("status") == "failed":
                if not retry_failed:
                    return
                previous.update(status="queued", not_before=0, owner=None, lease_until=0)
                previous.pop("error", None)
                previous.pop("error_type", None)
                store.put_record("job", key, previous)
            if previous is None:
                store.put_record("job", key, dict(key=key, kind=job.kind, session_id=job.session_id,
                    upto=job.upto, at=job.at.isoformat(), status="queued", attempts=0, not_before=0,
                    queued_at=time.time(), owner=None, lease_until=0))
        self._jobs.put(job)

    def _run(self, job):
        store = self.bg.store(job.user)
        key = self._job_key(job)
        claimed = False
        lease_stop = threading.Event()
        def renew():
            while not lease_stop.wait(30):
                try:
                    with store.transaction():
                        state = store.record("job", key)
                        if not state or state.get("owner") != self._worker_id or state.get("status") != "running":
                            return
                        state["lease_until"] = time.time() + 120
                        store.put_record("job", key, state)
                except Exception:
                    pass  # Consolidation's cursor/version check still rejects a competing publication.
        try:
            with store.transaction():
                state = store.record("job", key)
                if not state or state.get("status") in ("completed", "failed") or state.get("not_before", 0) > time.time():
                    return
                if state.get("status") == "running" and state.get("lease_until", 0) > time.time():
                    return
                state.update(status="running", owner=self._worker_id, lease_until=time.time() + 120,
                             started_at=time.time())
                store.put_record("job", key, state)
            claimed = True
            self.log("[%s] 开始整理会话 %s。" % (job.user, job.session_id))
            heartbeat = threading.Thread(target=renew, name="memsys-job-lease", daemon=True)
            heartbeat.start()
            if job.kind == "backfill":
                self.bg.rebuild(job.user)
                if store.backlog_count():
                    raise ValueError("extraction incomplete; source backlog retained")
            else:
                session = store.session(job.session_id)
                if not (job.kind == "close" and session["status"] == "closed"):
                    if job.kind == "close":
                        report, _ = self.bg.close_session(SessionHandle(job.user, job.session_id), job.at)
                    else:
                        report = Writer(store, self.bg.models, self.bg.settings).consolidate(job.session_id, upto_turn=job.upto)
                    self.log("[%s] 会话 %s 已整理。%s" % (job.user, job.session_id, report.summary()))
            with store.transaction():
                state = store.record("job", key)
                if state.get("owner") != self._worker_id:
                    raise WriteConflict("job lease changed")
                state.update(status="completed", owner=None, lease_until=0, not_before=0,
                             completed_at=time.time())
                state.pop("error", None)
                state.pop("error_type", None)
                store.put_record("job", key, state)
        except Exception as exc:
            permanent = (isinstance(exc, (BadJSONError, IncompleteJSONError)) or
                         isinstance(exc, ModelError) and exc.status is not None and exc.status not in RETRYABLE_STATUS)
            if claimed:
                try:
                    with store.transaction():
                        state = store.record("job", key)
                        if state and state.get("owner") == self._worker_id:
                            state.update(status="failed" if permanent else "retry", attempts=state.get("attempts", 0)+1,
                                         not_before=0 if permanent else time.time()+min(60*(state.get("attempts", 0)+1), 600),
                                         owner=None, lease_until=0, error_type=type(exc).__name__, error=str(exc)[:200])
                            store.put_record("job", key, state)
                except Exception:
                    pass  # Expired durable lease is recoverable on the next tick/restart.
                self.log("[%s] 整理失败，原文和任务保留：%s" % (job.user, str(exc)[:200]))
            else:
                self.log("[%s] 后台任务领取未完成：%s" % (job.user, str(exc)[:200]))
        finally:
            lease_stop.set()
            if job.kind == "flush":
                with self.lock(job.user):
                    if job.user in self._state:
                        self._state[job.user].flushing = False

    def _tick(self) -> None:
        now = self.clock()
        if self.idle:
            for user in list(self._state):
                with self.lock(user):
                    st = self._state[user]
                    if st.session is not None and st.last_active and now - st.last_active > self.idle:
                        self._close(user, st)
        from .store import Store
        for user in set(self._state) | set(Store.users(self.req.storage, client=self.req._qdrant_client)):
            store = self.req.store(user)
            for session in store.records("session"):
                if session["status"] == "closing":
                    self._enqueue(Job("close", user, session["id"], from_iso(session.get("close_requested_at") or session["opened_at"])))
            if self.bg.models.for_purpose("extract") is not None and store.backlog_count():
                self._enqueue(Job("backfill", user, "", now, upto=int(store.get_meta("backlog_revision", "0"))))
            for row in self.req.store(user).records("job"):
                if row.get("status") in ("queued", "retry") and row.get("not_before", 0) <= time.time() or row.get("status") == "running" and row.get("lease_until", 0) <= time.time():
                    self._jobs.put(Job(row["kind"], user, row["session_id"], from_iso(row["at"]), row.get("upto")))



# ------------------------------------------------------------ HTTP
class MemoryProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], service: MemoryService, upstream: Optional[str],
                 borrowed: Optional[Borrowed] = None, default_user: str = "default",
                 log: Callable[[str], None] = print, *, web_local_user=None):
        self.api_token = os.environ.get("MEMSYS_API_TOKEN", "")
        self.user_tokens = json.loads(os.environ.get("MEMSYS_USER_TOKENS", "{}") or "{}")
        if not isinstance(self.user_tokens, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in self.user_tokens.items()):
            raise ValueError("MEMSYS_USER_TOKENS must map user IDs to nonempty tokens")
        if len(set(self.user_tokens.values())) != len(self.user_tokens):
            raise ValueError("user tokens must be unique")
        if address[0] not in ("127.0.0.1", "localhost", "::1") and not (self.api_token or self.user_tokens):
            raise ValueError("network listening requires MEMSYS_API_TOKEN or MEMSYS_USER_TOKENS")
        if web_local_user and address[0] not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("automatic browser login requires loopback listening")
        super().__init__(address, _Handler)
        self.service = service
        self.upstream = upstream.rstrip("/") if upstream else None
        self.borrowed = borrowed
        self.default_user = default_user
        self.log = log
        from .webchat import WebChat
        self.web_chat = WebChat(service, local_user=web_local_user)

    def target(self, path: str) -> str:
        base = self.upstream or ""
        if base.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]
        return base + path


class _Handler(BaseHTTPRequestHandler):
    server: MemoryProxy
    server_version = "memsys"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _authorize(self):
        self.authenticated_user = None
        if not (self.server.api_token or self.server.user_tokens):
            return True
        token = self.headers.get("X-Memsys-Token", "")
        if not token and self.path.startswith("/memsys/"):
            token = self.headers.get("Authorization", "")[7:] if self.headers.get("Authorization", "").startswith("Bearer ") else ""
        if self.server.api_token and hmac.compare_digest(token, self.server.api_token):
            return True
        for user, expected in self.server.user_tokens.items():
            if hmac.compare_digest(token, expected):
                self.authenticated_user = user
                return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _scope_user(self, requested):
        bound = getattr(self, "authenticated_user", None)
        if bound and requested != bound:
            self._json(403, {"error": "token cannot access another user's memory"})
            return None
        return requested

    # -------------------------------------------------------------- routing
    def do_GET(self) -> None:
        from .webchat import route, static
        if static(self) or route(self):
            return
        if self.path == "/healthz":
            self._json(200, {"status": "ready"})
            return
        if not self._authorize():
            return
        if self.path.split("?")[0] == "/memsys/status":
            self._json(200, self.server.service.status(only_user=self.authenticated_user))
        else:
            self._forward_raw(None)

    def do_POST(self) -> None:
        from .webchat import route
        if route(self):
            return
        if not self._authorize():
            return
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= 4*1024*1024:
                raise ValueError("request too large")
        except ValueError:
            self._json(413, {"error": "invalid request size"})
            return
        raw = self.rfile.read(length)
        if path.startswith("/memsys/"):
            self._rest(path, raw)
        elif path.endswith("/chat/completions"):
            self._chat(raw, OpenAIChat)
        elif path.endswith("/messages"):
            self._chat(raw, AnthropicMessages)
        else:
            self._forward_raw(raw)

    # -------------------------------------------------------------- REST
    def _rest(self, path: str, raw: bytes) -> None:
        service = self.server.service
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            user = str(body.get("user") or self.headers.get("X-Memsys-User") or self.authenticated_user or self.server.default_user)
            user = self._scope_user(user)
            if user is None:
                return
            now = service.clock()
            if path == "/memsys/recall":
                query = str(body.get("query") or "").strip()
                if not query:
                    raise ValueError("query is required")
                rec = service.recall(user, query, now, budget=body.get("budget"), coverage_budget=body.get('coverage_budget'))
                self._json(200, {"context": rec.context, "memory": rec.memory, "rules": rec.rules,
                                 "items": rec.items, "trace_id": rec.trace_id, "tier": service.models.tier,
                                 "coverage": rec.trace.get("coverage"), "revision": rec.trace.get("revision"),
                                 "relations": rec.trace.get('relations'),
                                 "coverage_supplement": rec.trace.get('coverage_supplement')})
            elif path == "/memsys/recall-pages":
                query = str(body.get("query") or "").strip()
                if not query:
                    raise ValueError("query is required")
                self._json(200, service.req.recall_pages(user, query, now))
            elif path == "/memsys/history":
                facts = service.req.history(user, str(body.get("key") or ""))
                self._json(200, {"versions": [{"key": f.key, "text": f.text, "status": f.status,
                    "replaced_by": "F%d" % f.superseded_by if f.superseded_by else None,
                    "valid_from": f.valid_from.isoformat() if f.valid_from else None,
                    "valid_to": f.valid_to.isoformat() if f.valid_to else None} for f in facts]})
            elif path == "/memsys/sources":
                ids = []
                for key in body.get("keys", []):
                    kind, item_id = service.req._parse_key(str(key))
                    if kind != "episode":
                        raise ValueError("source keys must be episode IDs")
                    ids.append(item_id)
                episodes = service.req.store(user).episodes(ids, statuses=("active", "archived"))
                self._json(200, {"sources": [{"key": e.key, "session_id": e.session_id, "at": e.at.isoformat(),
                     "status": e.status, "user_text": e.user_text, "assistant_text": e.assistant_text} for e in episodes]})
            elif path == "/memsys/sessions":
                handle = service.req.open_session(user, now, external_id=body.get("external_id"))
                self._json(200, {"session_id": handle.session_id})
            elif path == "/memsys/turns":
                turns = body.get("turns")
                if not isinstance(turns, list):
                    raise ValueError("turns must be a list of {role, content}")
                if body.get("session_id"):
                    store = service.req.store(user)
                    indices = [store.append_turn(body["session_id"], t["role"], t["content"], now,
                               idempotency_key=t.get("idempotency_key")) for t in turns]
                    self._json(200, {"recorded": len(indices), "indices": indices})
                else:
                    self._json(200, {"recorded": service.add_turns(user, turns, now)})
            elif path == "/memsys/end":
                if body.get("session_id"):
                    service.req.store(user).session(body["session_id"])
                    service._enqueue(Job("close", user, body["session_id"], now))
                    self._json(200, {"ended": True})
                else:
                    self._json(200, {"ended": service.end(user)})
            elif path == "/memsys/forget":
                self._json(200, {"forgotten": service.forget(user, str(body.get("key") or ""))})
            elif path == "/memsys/retry":
                service.retry_job(user, str(body.get("job_key") or ""))
                self._json(200, {"queued": True})
            else:
                self._json(404, {"error": "unknown endpoint %s" % path})
        except (ValueError, KeyError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    # -------------------------------------------------------------- proxy
    def _chat(self, raw: bytes, proto: Any) -> None:
        if not self.server.upstream:
            self._json(404, {"error": "no --upstream configured; only /memsys/* endpoints are available"})
            return
        try:
            body = json.loads(raw)
        except ValueError:
            self._forward_raw(raw)
            return
        if not isinstance(body, dict):
            self._forward_raw(raw)
            return
        service = self.server.service
        user = self.headers.get("X-Memsys-User") or proto.user_id(body) or self.authenticated_user or self.server.default_user
        user = self._scope_user(user)
        if user is None:
            return
        if self.server.borrowed is not None:
            self.server.borrowed.update(dict(self.headers), body.get("model"), proto.api)
        trace_id = None
        use_memory = (self.headers.get("X-Memsys-Memory") or "").lower() != "off"
        found = proto.last_user(body) if use_memory else None
        if found:
            index, text, count = found
            key = "%d:%s" % (count, hashlib.sha1(text.encode("utf-8")).hexdigest()[:16])
            try:
                now = service.clock()
                service.record_user(user, text, key, now)
                rec = service.recall(user, text, now, key=key)
                proto.inject(body, index, "<memory>\n%s\n</memory>" % rec.context)
                trace_id = rec.trace_id
            except Exception as exc:   # 记忆出错不能让 agent 跟着出错：这一轮不带记忆照常转发
                self.server.log("[%s] 这一轮没有带上记忆：%s: %s" % (user, type(exc).__name__, str(exc)[:200]))
        def record(reply: str) -> None:
            if not found:
                return
            try:
                service.record_assistant(user, reply, service.clock())
            except Exception as exc:
                self.server.log("[%s] 回答没有记下：%s" % (user, exc))

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._forward(payload, stream=bool(body.get("stream")), proto=proto, trace_id=trace_id, on_final=record)

    def _upstream_request(self, payload: Optional[bytes]) -> urllib.request.Request:
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in _HOP and not k.lower().startswith("x-memsys-")}
        headers["Accept-Encoding"] = "identity"
        return urllib.request.Request(self.server.target(self.path), data=payload, headers=headers,
                                      method=self.command)

    def _forward(self, payload: bytes, stream: bool, proto: Any, trace_id: Optional[str],
                 on_final: Callable[[str], None]) -> None:
        """转发并把回复原样转回。这一轮的最终回答（不是中间的工具调用）交给 on_final 记下：
        非流式在回给 agent 之前记，流式在流结束、连接关闭之前记。"""
        try:
            resp = urllib.request.urlopen(self._upstream_request(payload), timeout=600)
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.headers, exc.read(), trace_id)
            return
        except OSError as exc:
            self._json(502, {"error": "upstream unreachable: %s" % exc})
            return
        with resp:
            if not stream or "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
                data = resp.read()
                try:
                    parsed = json.loads(data)
                except ValueError:
                    parsed = None
                final = proto.final_text(parsed) if isinstance(parsed, dict) and resp.status == 200 else None
                if final:
                    on_final(final)
                self._send(resp.status, resp.headers, data, trace_id)
                return
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in _RESPONSE_SKIP:
                    self.send_header(k, v)
            if trace_id:
                self.send_header("X-Memsys-Trace", trace_id)
            self.end_headers()
            parts: List[str] = []
            reason: Optional[str] = None
            while True:
                line = resp.readline()
                if not line:
                    break
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except OSError:      # agent 那边断开了：不再转发，也不记半截的回答
                    return
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("data:"):
                    data_text = text[5:].strip()
                    if data_text and data_text != "[DONE]":
                        try:
                            event = json.loads(data_text)
                        except ValueError:
                            continue
                        delta, finish = proto.stream_event(event)
                        parts.append(delta)
                        reason = finish or reason
            answer = "".join(parts).strip()
            if answer and proto.finished(reason):
                on_final(answer)

    def _forward_raw(self, payload: Optional[bytes]) -> None:
        if not self.server.upstream:
            self._json(404, {"error": "unknown endpoint %s" % self.path})
            return
        try:
            with urllib.request.urlopen(self._upstream_request(payload), timeout=600) as resp:
                self._send(resp.status, resp.headers, resp.read(), None)
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.headers, exc.read(), None)
        except OSError as exc:
            self._json(502, {"error": "upstream unreachable: %s" % exc})

    def _send(self, status: int, headers: Any, data: bytes, trace_id: Optional[str]) -> None:
        self.send_response(status)
        for k, v in (headers.items() if headers else []):
            if k.lower() not in _RESPONSE_SKIP:
                self.send_header(k, v)
        if trace_id:
            self.send_header("X-Memsys-Trace", trace_id)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
