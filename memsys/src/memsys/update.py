"""确定性的新增与精确去重；不调用模型，不判断新旧事实关系。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Tuple

from .config import MemorySettings
from .schema import Fact
from .fact_content import fact_identity
from .memory_compaction import unanchored_event
from .store import Store

if TYPE_CHECKING:
    from .write import NewFact, WriteReport

CONFIDENCE = {"firm": 0.9, "tentative": 0.5}
DEFAULT_CONFIDENCE = 0.8


def certainty_to_confidence(value: Any) -> float:
    return CONFIDENCE.get(str(value or "").strip().lower(), DEFAULT_CONFIDENCE)


def is_tentative(fact: Fact, settings: MemorySettings) -> bool:
    return fact.confidence < settings.tentative_threshold


class MemoryInserter:
    def __init__(self, store: Store):
        self.store = store

    def add(self, fact: "NewFact", session_id: str, session_at: datetime,
            episode_ids: List[int], report: "WriteReport") -> Tuple[int, bool]:
        """在提交内去重；返回事实编号及是否新增。不同文本始终新增。"""
        old = self.store.exact_fact(fact.text, identity=fact_identity(fact))
        if old is not None and unanchored_event(fact):
            # Without an occurrence date or ID, equal wording does not prove
            # two different source observations describe the same event.
            if old.session_id != session_id or set(self.store.sources([old.id])[old.id]) != set(episode_ids):
                old = None
        if old is not None:
            # 只关联此次原文，不强化置信度，也不改变旧事实的内容或状态。
            self.store.note_duplicate(old.id, episode_ids, old.confidence)
            report.duplicates += 1
            return old.id, False
        fact_id = self.store.add_fact(
            text=fact.text, kind=fact.kind, subject=fact.subject, attribute=fact.attribute, value=fact.value,
            event=fact.event, recorded_at=session_at,
            valid_from=fact.event.start if fact.event else session_at.date(), session_id=session_id,
            importance=fact.importance, confidence=fact.confidence,
            episode_ids=episode_ids, semantics=fact.semantics, relations=fact.relations)
        report.added.append(fact_id)
        return fact_id, True
