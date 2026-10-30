"""LongMemEval 数据加载，带字段白名单。

数据集里有标准答案（answer）、题型（question_type）、证据标记（has_answer）和证据会话
（answer_session_ids）。这些一旦进了记忆系统就是"偷看答案"，属于手册 F.1 说的特判。
所以加载时把每道题拆成两部分：

- ReplayItem：记忆系统唯一能看到的东西，只有问题、提问时间和按时间排好序的历史会话（角色和内容）
- GoldLabel：只给评分和报告用，永远不传给记忆系统
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple

from ..schema import Turn
from ..timeutil import parse_datetime


@dataclass(frozen=True)
class HistorySession:
    session_id: str
    date: datetime
    turns: Tuple[Turn, ...]


@dataclass(frozen=True)
class ReplayItem:
    question_id: str
    question: str
    question_date: datetime
    sessions: Tuple[HistorySession, ...]   # 已按时间排序；Oracle 档原始数据是乱序的


@dataclass(frozen=True)
class GoldLabel:
    question_id: str
    question_type: str
    question: str
    answer: str

    @property
    def is_abstention(self) -> bool:
        return self.question_id.endswith("_abs")


def load(path: str) -> List[Tuple[ReplayItem, GoldLabel]]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        entries = json.loads(text)
    except json.JSONDecodeError:
        entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [_split(entry) for entry in entries]


def _split(entry: dict) -> Tuple[ReplayItem, GoldLabel]:
    lengths = [len(entry[k]) for k in ("haystack_session_ids", "haystack_dates", "haystack_sessions")]
    if len(set(lengths)) != 1:
        raise ValueError("history IDs, dates and sessions have different lengths; refusing silent zip truncation")
    sessions = []
    for order, (session_id, date_text, turns) in enumerate(
            zip(entry["haystack_session_ids"], entry["haystack_dates"], entry["haystack_sessions"])):
        if any(t.get("role") not in ("user", "assistant") for t in turns):
            raise ValueError("history contains an unsupported speaker role")
        sessions.append((parse_datetime(date_text), order, HistorySession(
            session_id=str(session_id), date=parse_datetime(date_text),
            turns=tuple(Turn(str(t["role"]), str(t["content"])) for t in turns))))
    sessions.sort(key=lambda item: (item[0], item[1]))
    item = ReplayItem(question_id=str(entry["question_id"]), question=str(entry["question"]),
                      question_date=parse_datetime(entry["question_date"]),
                      sessions=tuple(s for _, _, s in sessions))
    gold = GoldLabel(question_id=str(entry["question_id"]), question_type=str(entry.get("question_type", "")),
                     question=str(entry["question"]), answer=str(entry.get("answer", "")))
    return item, gold
