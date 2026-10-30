"""巩固与遗忘：按保留分把长期不用的记忆降级归档，超过容量上限时再淘汰分数最低的。

遗忘是"降级"而不是"删除"：归档的记忆不参与默认检索，但相关记忆太少时会被兜底检索找回；
只有用户主动 forget 的条目才彻底不再参与检索（数据仍留在库里，可追溯；连带规则见 Store.forget）。

触发时机：每次会话整理完之后（MemorySystem.close_session），以及时间推进时（MemorySystem.maintain）。

保留分 = 重要性 × w_imp + 时近度 × w_rec + 使用频率 × w_use；事实再乘以 (0.6 + 0.4 × 置信度)
- 重要性：事实取抽取时的 importance，原文轮次取门控的显著性
- 时近度：距最近一次被用到（没用过则距记录时间）的天数，按半衰期指数衰减
- 使用频率：被放进上下文的次数，log 压缩到 0~1
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

from .config import MemorySettings
from .schema import ARCHIVED, EPISODE_ACTIVE, FACT_CURRENT, FACT_SUPERSEDED, Episode, Fact
from .store import Store

W_IMPORTANCE, W_RECENCY, W_USAGE = 0.45, 0.35, 0.20


@dataclass
class LifecycleReport:
    archived_facts: List[int] = field(default_factory=list)
    archived_episodes: List[int] = field(default_factory=list)

    def summary(self) -> str:
        if not (self.archived_facts or self.archived_episodes):
            return ""
        return "遗忘：归档 %d 条事实、%d 轮原文" % (len(self.archived_facts), len(self.archived_episodes))


def retention(item: Union[Fact, Episode], now: datetime, settings: MemorySettings) -> float:
    if isinstance(item, Fact):
        importance, born = item.importance, item.recorded_at
    else:
        importance, born = item.salience, item.at
    last_seen = item.last_access or born
    age_days = max((now - last_seen).total_seconds() / 86400.0, 0.0)
    recency = 0.5 ** (age_days / settings.half_life_days)
    usage = min(math.log1p(item.access_count) / math.log1p(10), 1.0)
    score = W_IMPORTANCE * importance + W_RECENCY * recency + W_USAGE * usage
    if isinstance(item, Fact):
        score *= 0.6 + 0.4 * item.confidence   # 只是打算、拿不准的事实衰减得更快
        if item.status == FACT_SUPERSEDED:
            score *= 0.5
    return score


def _idle_days(item: Union[Fact, Episode], now: datetime) -> float:
    last_seen = item.last_access or (item.recorded_at if isinstance(item, Fact) else item.at)
    return (now - last_seen).total_seconds() / 86400.0


def run_lifecycle(store: Store, now: datetime, settings: MemorySettings) -> LifecycleReport:
    with store.transaction():
        report = LifecycleReport()
        facts = store.facts(statuses=(FACT_CURRENT, FACT_SUPERSEDED))
        episodes = store.episodes(statuses=(EPISODE_ACTIVE,))

        # 1. 被取代很久的旧事实归档（版本链仍在，问"以前"时可兜底找回）
        for fact in facts:
            if fact.status == FACT_SUPERSEDED and fact.valid_to is not None and \
                    (now.date() - fact.valid_to).days >= settings.superseded_archive_days:
                report.archived_facts.append(fact.id)

        # 2. 长期没被用到、保留分又低的条目归档。仍被当前事实引用的原文轮次不动
        linked = store.facts_of_episodes([e.id for e in episodes])
        current_ids = {f.id for f in facts if f.status == FACT_CURRENT}
        for episode in episodes:
            if any(fid in current_ids for fid in linked.get(episode.id, [])):
                continue
            if _idle_days(episode, now) >= settings.archive_after_days and \
                    retention(episode, now, settings) < settings.archive_threshold:
                report.archived_episodes.append(episode.id)
        for fact in facts:
            if fact.id in report.archived_facts or fact.status != FACT_CURRENT:
                continue
            if _idle_days(fact, now) >= settings.archive_after_days and \
                    retention(fact, now, settings) < settings.archive_threshold:
                report.archived_facts.append(fact.id)

        # 3. 容量上限：超出部分按保留分从低到高归档
        def over_capacity(items: List[Union[Fact, Episode]], archived: List[int],
                          cap: Optional[int]) -> List[int]:
            alive = [i for i in items if i.id not in archived]
            if cap is None or len(alive) <= cap:
                return []
            ranked: List[Tuple[float, int]] = sorted((retention(i, now, settings), i.id) for i in alive)
            return [item_id for _, item_id in ranked[:len(alive) - cap]]

        report.archived_facts += over_capacity(facts, report.archived_facts, settings.max_active_facts)
        report.archived_episodes += over_capacity(episodes, report.archived_episodes, settings.max_active_episodes)

        if report.archived_facts or report.archived_episodes:
            with store.transaction():
                store.set_status("fact", report.archived_facts, ARCHIVED)
                store.set_status("episode", report.archived_episodes, ARCHIVED)
        return report


def retention_table(store: Store, now: datetime, settings: MemorySettings) -> Dict[str, float]:
    """给 /stats 用：每条活跃记忆的保留分。"""
    table = {f.key: retention(f, now, settings) for f in store.facts(statuses=(FACT_CURRENT, FACT_SUPERSEDED))}
    table.update({e.key: retention(e, now, settings) for e in store.episodes(statuses=(EPISODE_ACTIVE,))})
    return table
