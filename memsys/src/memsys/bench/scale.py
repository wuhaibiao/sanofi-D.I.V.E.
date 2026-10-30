"""检索延迟随记忆规模的变化（手册 B.2"效率"）。

LongMemEval Oracle 每题只有几十到几百条记忆，看不出规模效应。这里用合成数据把一个记忆库
灌到指定条数，再用不调模型的检索计划反复检索，测"检索 + 预算选取"这一段纯本地路径的耗时。
向量用本地哈希向量，维度可以设成和真实 embedding 模型一样（bge-m3 是 1024），
所以打分的计算量和真实情况一致；不联网、不花钱、结果可复现。
"""

from __future__ import annotations

import os
import random
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Sequence

from ..config import MemorySettings, Models
from ..fake import HashEmbedder
from ..read import QueryPlan, Retriever
from ..schema import EPISODE_ACTIVE
from ..store import Store
from ..textutil import terms
from ..timeutil import DateSpan
from ..vecmath import backend_name

_TOPICS = ("cooking", "hiking", "piano", "garden", "marathon", "camera", "violin", "yoga", "pottery", "chess",
           "kayak", "novel", "podcast", "museum", "bakery", "laptop", "bicycle", "aquarium", "concert", "sushi")
_VERBS = ("bought", "visited", "practiced", "booked", "finished", "started", "planned", "returned", "tried", "sold")


@dataclass
class ScaleRow:
    items: int
    facts: int
    episodes: int
    search_ms_p50: float
    search_ms_p95: float
    select_ms_p50: float
    db_mb: float


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)]


def _sentence(rng: random.Random, i: int) -> str:
    topic, verb = rng.choice(_TOPICS), rng.choice(_VERBS)
    return "I %s a %s item number %d with %s near %s on day %d" % (
        verb, topic, i, rng.choice(_TOPICS), rng.choice(_TOPICS), rng.randint(1, 365))


def build_store(path: str, items: int, embedder: HashEmbedder, seed: int = 0) -> Store:
    """灌入 items 条记忆：一半事实、一半原文轮次，每条事实指向一轮原文。"""
    rng = random.Random(seed)
    store = Store(path)
    start = datetime(2023, 1, 1, 9, 0)
    n_episodes = items - items // 2
    n_facts = items // 2
    batch = 500
    with store.transaction():
        session = store.create_session(start)
    episode_ids: List[int] = []
    for offset in range(0, n_episodes, batch):
        texts = [_sentence(rng, offset + i) for i in range(min(batch, n_episodes - offset))]
        vectors = embedder.embed(["User: %s" % t for t in texts])
        with store.transaction():
            ids = [store.add_episode(session, offset + i, start + timedelta(hours=offset + i), text,
                                     "Noted.", 0.5, EPISODE_ACTIVE) for i, text in enumerate(texts)]
            store.put_vectors("episode", list(zip(ids, vectors)))
        episode_ids += ids
    for offset in range(0, n_facts, batch):
        texts = ["The user " + _sentence(rng, offset + i)[2:] for i in range(min(batch, n_facts - offset))]
        vectors = embedder.embed(texts)
        with store.transaction():
            ids = []
            for i, text in enumerate(texts):
                day = (start + timedelta(hours=offset + i)).date()
                ids.append(store.add_fact(
                    text=text, kind="event", subject="user", attribute="", value=text, event=DateSpan.day(day),
                    recorded_at=start + timedelta(hours=offset + i), valid_from=day, session_id=session,
                    importance=0.6, episode_ids=[episode_ids[(offset + i) % len(episode_ids)]] if episode_ids else []))
            store.put_vectors("fact", list(zip(ids, vectors)))
    with store.transaction():
        store.mark_consolidated(session, 0, start)
    return store


def measure(sizes: Sequence[int], dim: int = 1024, queries: int = 30, seed: int = 0,
            settings: Optional[MemorySettings] = None,
            progress: Optional[Callable[[str], None]] = None) -> List[ScaleRow]:
    settings = settings or MemorySettings(llm_rerank=False, query_analysis=False)
    embedder = HashEmbedder(dim=dim)
    models = Models(chat=None, memory=None, embedder=embedder, embedding_name="hash-%d" % dim)  # type: ignore[arg-type]
    rows = []
    rng = random.Random(seed + 1)
    with tempfile.TemporaryDirectory() as tmp:
        for size in sizes:
            path = os.path.join(tmp, "scale-%d.sqlite" % size)
            store = build_store(path, size, embedder, seed)
            store.check_embedding_model(models.embedding_name)
            retriever = Retriever(store, models, settings)
            search_ms, select_ms = [], []
            for _ in range(queries):
                question = "When did I %s the %s?" % (rng.choice(_VERBS), rng.choice(_TOPICS))
                plan = QueryPlan([question, "%s %s" % (rng.choice(_TOPICS), rng.choice(_TOPICS))],
                                 terms(question), None, False, "any", "heuristic")
                t0 = time.perf_counter()
                result = retriever.search(plan)
                t1 = time.perf_counter()
                retriever.select(result, plan)
                t2 = time.perf_counter()
                search_ms.append((t1 - t0) * 1000)
                select_ms.append((t2 - t1) * 1000)
            counts = store.counts()
            store.close()
            rows.append(ScaleRow(size, sum(counts["fact"].values()), sum(counts["episode"].values()),
                                 _quantile(search_ms, .5), _quantile(search_ms, .95), _quantile(select_ms, .5),
                                 os.path.getsize(path) / 1e6))
            if progress:
                progress("%d 条：检索 P50 %.1f ms" % (size, rows[-1].search_ms_p50))
    return rows


def to_markdown(rows: Sequence[ScaleRow], dim: int, queries: int) -> str:
    lines = ["## 检索延迟随规模的变化（合成数据）", "",
             "向量维度 %d（本地哈希向量，计算量与同维度的真实 embedding 相同），每个规模 %d 次检索，"
             "每次 2 条检索语句；只测本地检索与预算选取，不含查询分析、重排和生成这些模型调用。" % (dim, queries), "",
             "| 记忆条数 | 事实 | 原文轮次 | 检索 P50 | 检索 P95 | 预算选取 P50 | 库文件 |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %d | %d | %d | %.1f ms | %.1f ms | %.2f ms | %.1f MB |" % (
            r.items, r.facts, r.episodes, r.search_ms_p50, r.search_ms_p95, r.select_ms_p50, r.db_mb))
    lines += ["", "向量打分后端：%s；精确线性扫描，耗时随条数增长。LongMemEval Oracle 每题只有几十到几百条记忆。" % backend_name(), ""]
    return "\n".join(lines)
