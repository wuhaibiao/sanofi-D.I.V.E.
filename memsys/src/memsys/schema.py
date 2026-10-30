"""记忆的数据模型。

- Episode（情景记忆）：一"轮"对话的原文，即用户一句加助手的回复，带会话时间。回答时的依据。
- Fact（语义记忆）：从对话里抽取出的原子事实，带事件时间、记录时间、有效区间和版本链。
  它既是检索时被命中的"键"（指回来源轮次），也是"当前真相"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from .timeutil import DateSpan

FACT_KINDS = ("personal", "event", "preference", "assistant")
VALUE_TYPES = {"state", "cumulative", "increment", "event_quantity", "other", "unknown"}
EVENT_STATUSES = {"planned", "ongoing", "completed", "cancelled", "unknown"}
CONTEXT_ROLES = {'goal', 'constraint', 'resource', 'experience', 'preference', 'question'}


def normalize_semantics(value):
    """Normalize annotations without silently clipping names, conditions or times."""
    if not isinstance(value, dict):
        return {}
    kind = value.get("value_type", "unknown")
    normalized = {"value_type": kind if isinstance(kind, str) and kind in VALUE_TYPES else "unknown",
            "object_key": str(value.get("object_key") or "").strip().casefold(),
            "event_key": str(value.get("event_key") or "").strip().casefold(),
            "qualifiers": list(dict.fromkeys(q.strip().casefold() for q in value.get("qualifiers", []) if isinstance(q, str) and q.strip()))
                          if isinstance(value.get("qualifiers"), list) else [],
            "relative_time": str(value.get("relative_time") or "").strip()}
    # Absence remains absence for legacy identity compatibility. Never infer
    # completion from confidence, recording time or the presence of an event date.
    status = value.get('event_status')
    if isinstance(status, str) and status.strip().casefold() in EVENT_STATUSES:
        normalized['event_status'] = status.strip().casefold()
    role = value.get('context_role')
    if isinstance(role, str) and role.strip().casefold() in CONTEXT_ROLES:
        normalized['context_role'] = role.strip().casefold()
    task = value.get('task_key')
    if isinstance(task, str) and task.strip():
        normalized['task_key'] = task.strip().casefold()
    return normalized

# 状态：只有 SEARCHABLE 里的状态默认参与检索；archived 只在兜底检索时用；forgotten 永不参与
EPISODE_ACTIVE, EPISODE_SKIPPED = "active", "skipped"
EPISODE_WITHHELD = "withheld"  # 包含主动遗忘事实的共享原文，不进入检索、补抽或重建索引
FACT_CURRENT, FACT_SUPERSEDED = "current", "superseded"
ARCHIVED, FORGOTTEN = "archived", "forgotten"

SEARCHABLE = {"fact": (FACT_CURRENT, FACT_SUPERSEDED), "episode": (EPISODE_ACTIVE,)}
SEARCHABLE_WITH_ARCHIVE = {"fact": (FACT_CURRENT, FACT_SUPERSEDED, ARCHIVED),
                           "episode": (EPISODE_ACTIVE, ARCHIVED)}


@dataclass(frozen=True)
class Turn:
    role: str      # "user" 或 "assistant"
    content: str


@dataclass(frozen=True)
class EpisodeChunk:
    id: int
    episode_id: int
    ordinal: int
    role: str
    start: int
    end: int


@dataclass
class Episode:
    id: int
    session_id: str
    round_idx: int
    at: datetime
    user_text: str
    assistant_text: str
    salience: float
    status: str
    access_count: int = 0
    last_access: Optional[datetime] = None

    @property
    def key(self) -> str:
        return "E%d" % self.id

    def text(self) -> str:
        parts = []
        if self.user_text:
            parts.append("User: " + self.user_text)
        if self.assistant_text:
            parts.append("Assistant: " + self.assistant_text)
        return "\n".join(parts)


@dataclass
class Fact:
    id: int
    text: str
    kind: str
    subject: str
    attribute: str
    value: str
    event: Optional[DateSpan]       # 事件发生（或计划发生）的时间
    recorded_at: datetime           # 用户在哪次会话里说的
    valid_from: Optional[date]
    valid_to: Optional[date]        # 被新事实取代的时间；当前有效的为 None
    status: str
    superseded_by: Optional[int]
    uncertain: bool
    confidence: float
    importance: float
    session_id: str
    mentions: int = 1
    access_count: int = 0
    last_access: Optional[datetime] = None
    semantics: Dict[str, object] = field(default_factory=dict)
    relations: List[Dict[str, object]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return "F%d" % self.id

    def when(self) -> DateSpan:
        """用于时间过滤和排序的日期：有事件时间用事件时间，否则用记录时间。"""
        return self.event or DateSpan.day(self.recorded_at.date())
