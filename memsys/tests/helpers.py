"""测试公用的小工具：脚本化模型、时间、快速建库。"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import datetime
from typing import Dict, Iterator, List, Optional

from memsys.config import MemorySettings, Models
from memsys.fake import HashEmbedder, ScriptedChat


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def models(memory: Optional[Dict] = None, chat: Optional[Dict] = None) -> Models:
    return Models(chat=ScriptedChat(chat or {}), memory=ScriptedChat(memory or {}), embedder=HashEmbedder(),
                  embedding_name="hash-256", names={"chat": "scripted", "memory": "scripted"})


def facts_json(*facts: Dict) -> str:
    return json.dumps({"facts": list(facts)})


def fact(text: str, rounds: List[int], attribute: str = "", event_date: Optional[str] = None,
         kind: str = "personal", importance: int = 3, subject: str = "user", value: str = "") -> Dict:
    return {"text": text, "kind": kind, "subject": subject, "attribute": attribute, "value": value or text,
            "event_date": event_date, "rounds": rounds, "importance": importance}


def decisions_json(*items) -> str:
    return json.dumps({"decisions": [{"new": n, "action": a, "target": t, "reason": "test"} for n, a, t in items]})


def settings(**overrides) -> MemorySettings:
    # HashEmbedder 的相似度普遍偏低，测试里放宽阈值
    # 重排默认关掉，让检索相关的测试结果确定；重排有单独的测试
    # Legacy arithmetic tests exercise the low-level path; semantic integration
    # tests explicitly enable the production-default semantic review.
    base = dict(min_similarity=0.05, resolve_similarity=0.5, rerank_backend="llm", llm_rerank=False, budget_mode="estimate",
                semantic_validation=False)
    base.update(overrides)
    return MemorySettings(**base)


PROVIDER_KEYS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "SILICONFLOW_API_KEY",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "MEMSYS_LLM_BASE_URL", "MEMSYS_LLM_MODEL",
                 "MEMSYS_LLM_API_KEY", "MEMSYS_EMBED_BASE_URL", "MEMSYS_EMBED_MODEL", "MEMSYS_EMBED_API_KEY",
                 "MEMSYS_CONFIG", "MEMSYS_USER")


@contextlib.contextmanager
def isolated_env(home: str, **extra: str) -> Iterator[None]:
    """测试不能读到开发机上的 key、~/.memsys 和本机 Ollama：自动发现模型时只看这里给的环境。"""
    saved = {k: os.environ.get(k) for k in PROVIDER_KEYS + ("MEMSYS_HOME", "OLLAMA_HOST") + tuple(extra)}
    for key in PROVIDER_KEYS:
        os.environ.pop(key, None)
    os.environ["MEMSYS_HOME"] = home
    os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"     # 没有服务的端口，探测立刻失败
    os.environ.update(extra)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
