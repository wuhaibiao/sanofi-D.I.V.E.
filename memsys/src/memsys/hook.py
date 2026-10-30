"""Claude Code 的 hook 接入：memsys hook。由宿主每轮自动记录对话、自动注入记忆，不靠模型自己记得去调工具。

Claude Code 每触发一次 hook 就运行一次这个命令，事件 JSON 从标准输入进来：

- SessionStart：把上次没正常结束的 Claude Code 会话整理掉
- UserPromptSubmit：记下用户这句话，检索记忆，打印到标准输出（Claude Code 会把它加进这一轮的上下文）
- Stop：从对话记录（transcript）里取出这一轮的最终回答记下来
- SessionEnd：结束会话，整理进长期记忆

Claude Code 的会话 id 记成记忆会话的外部编号（claude-code:<id>），多次调用之间靠它接上。
配置写法见 README 的"接入 Claude Code"。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Mapping, Optional

from .core import MemorySystem, SessionHandle
from .serve import _text_of
from .timeutil import from_iso

PREFIX = "claude-code:"


def last_assistant_text(transcript_path: str) -> Optional[str]:
    """对话记录是 JSONL。取最后一条真正的用户输入（不是工具结果）之后，助手说的所有文字。"""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return None
    parts: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") or {}
        role = entry.get("type") or message.get("role")
        if role == "user" and not entry.get("isMeta") and _text_of(message.get("content")):
            parts = []                       # 新的一轮从这里开始
        elif role == "assistant":
            text = _text_of(message.get("content"))
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip() or None


def _session(system: MemorySystem, user: str, cc_session: str, now: datetime, create: bool) -> Optional[SessionHandle]:
    store = system.store(user)
    session_id = store.find_open_session(PREFIX + cc_session)
    if session_id is not None:
        return SessionHandle(user, session_id)
    return system.open_session(user, now, PREFIX + cc_session) if create else None


def handle(event: Mapping[str, Any], system: MemorySystem, user: str, now: datetime) -> str:
    """处理一个 hook 事件，返回要打印到标准输出的内容（只有 UserPromptSubmit 有）。"""
    name = event.get("hook_event_name")
    cc_session = str(event.get("session_id") or "default")
    store = system.store(user)
    if name == "UserPromptSubmit":
        prompt = str(event.get("prompt") or "").strip()
        if not prompt:
            return ""
        session = _session(system, user, cc_session, now, create=True)
        system.add_turn(session, "user", prompt, now)
        return system.recall(user, prompt, now).context
    if name == "Stop":
        text = event.get("last_assistant_message")
        if not isinstance(text, str) or not text.strip():
            text = last_assistant_text(str(event.get("transcript_path") or ""))
        session = _session(system, user, cc_session, now, create=False)
        if text and session is not None:
            last = store.last_turn(session.session_id)
            if last is None or last["role"] != "assistant" or last["content"] != text:
                system.add_turn(session, "assistant", text, now)
        return ""
    if name == "SessionEnd":
        session = _session(system, user, cc_session, now, create=False)
        if session is not None:
            system.close_session(session, now)
        return ""
    if name == "SessionStart":
        # 上次 Claude Code 被直接关掉、没触发 SessionEnd 的会话：现在整理
        for session_id in store.open_sessions():
            external = store.session(session_id)["external_id"] or ""
            if external.startswith(PREFIX) and external != PREFIX + cc_session:
                last = store.last_turn(session_id)
                at = from_iso(last["at"]) if last else now
                system.close_session(SessionHandle(user, session_id), at)
        return ""
    return ""
