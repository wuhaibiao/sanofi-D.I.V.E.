"""评测数据泄漏检查：提示词和代码里不能出现评测题的内容（手册 F.1：不得针对评测题硬编码、不得对测试集过拟合）。

检查三类东西：
1. 题号、题目原文、较长的标准答案，是否出现在源码、文档和测试里
2. 提示词里的专有名称（几个首字母大写的词连在一起，例如活动名、店名），是否出现在评测数据的对话或题目里
3. 提示词里带引号的长示例句，是否原样出现在评测数据里

数据集不进仓库，检查只在本地有数据时运行。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Sequence

from .. import prompts

_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+(?:for|of|the|and|on|in|at|de|la)?\s*[A-Z][a-z]+)+\b")
_QUOTED = re.compile(r'"([^"\n{}]{25,})"')


@dataclass
class Leak:
    where: str
    kind: str
    text: str


def _sub_names(match: str) -> List[str]:
    """"Walk for Hunger on February" → 所有首尾都是大写词、至少含两个大写词的连续片段。"""
    words = match.split()
    out = []
    for i in range(len(words)):
        for j in range(i + 1, len(words)):
            span = words[i:j + 1]
            if span[0][0].isupper() and span[-1][0].isupper() and sum(w[0].isupper() for w in span) >= 2:
                out.append(" ".join(span))
    return out


def prompt_texts() -> List[str]:
    from ..aggregate_batch import RULES, SEMANTIC_PAGE_RULES
    from ..evidence_batch import RULES as EVIDENCE_RULES
    from ..semantic_review import RECONCILE_RULES, SELECTION_RULES, RESPONSE_RULES
    return [v for k, v in vars(prompts).items() if k.isupper() and isinstance(v, str)] + [
        RULES, SEMANTIC_PAGE_RULES, EVIDENCE_RULES, RECONCILE_RULES, SELECTION_RULES, RESPONSE_RULES]


def check(data_path: str, files: Iterable[str]) -> List[Leak]:
    with open(data_path, encoding="utf-8") as fh:
        data = json.load(fh)
    corpus = " ".join([x["question"] for x in data] +
                      [t["content"] for x in data for s in x["haystack_sessions"] for t in s]).lower()
    leaks: List[Leak] = []
    ids = {str(x["question_id"]) for x in data}
    questions = {x["question"].strip().lower() for x in data if len(x["question"].strip()) >= 20}
    answers = {str(x["answer"]).strip().lower() for x in data if len(str(x["answer"]).strip()) >= 12}
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        lower = text.lower()
        for qid in ids:
            if re.search(r"\b%s\b" % re.escape(qid), text):
                leaks.append(Leak(path, "question_id", qid))
        leaks += [Leak(path, "question", q[:80]) for q in questions if q in lower]
        leaks += [Leak(path, "answer", a[:80]) for a in answers if a in lower]
    for text in prompt_texts():
        found = {name for match in _NAME.findall(text) for name in _sub_names(match) if name.lower() in corpus}
        # 只报最长的那个：Walk for Hunger 命中了，就不再单独报 Walk for
        leaks += [Leak("prompts.py", "name", n) for n in sorted(found)
                  if not any(n != other and n in other for other in found)]
        for quoted in set(_QUOTED.findall(text)):
            sample = quoted.rstrip(". …").strip().lower()
            if sample in corpus:
                leaks.append(Leak("prompts.py", "example", quoted[:80]))
    return leaks


def project_files(root: str) -> List[str]:
    out = []
    for base in ("src", "docs", "tests", "scripts"):
        for dirpath, _, names in os.walk(os.path.join(root, base)):
            out += [os.path.join(dirpath, n) for n in names if n.endswith((".py", ".md", ".txt", ".json"))]
    readme = os.path.join(root, "README.md")
    return out + ([readme] if os.path.exists(readme) else [])


def to_text(leaks: Sequence[Leak]) -> str:
    if not leaks:
        return "没有发现评测数据泄漏：题号、题目、答案没有出现在代码和文档里，提示词里的名称和例句也不在评测数据中。"
    return "\n".join(["发现 %d 处可能的泄漏：" % len(leaks)] +
                     ["  %s · %s · %s" % (l.where, l.kind, l.text) for l in leaks])
