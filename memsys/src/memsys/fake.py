"""不联网的替身模型，用于测试和 --fake 演练。

- HashEmbedder：特征哈希的词袋向量，确定性、无依赖，词面相近的文本向量相近。
- ScriptedChat：按 purpose 返回预设回复，单元测试用。
- HeuristicChat：用简单规则"扮演"各环节的模型，让整条流水线不用 API key 也能跑通。
  它的回答质量没有意义，只用来检查流程、文件和日志是否正常。
"""

from __future__ import annotations

import json
import re
import zlib
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Union

from .llm import ChatResult, Message
from .textutil import terms

Handler = Union[str, List[str], Callable[[Sequence[Message]], str]]


class HashEmbedder:
    def __init__(self, dim: int = 256):
        self.dim = dim
        self.calls = 0

    def embed(self, texts: Sequence[str], *, purpose: str = "embed",
              tags: Optional[Mapping[str, str]] = None) -> List[List[float]]:
        self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for term in terms(text):
                h = zlib.crc32(term.encode("utf-8"))
                vec[h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


def _result(text: str) -> ChatResult:
    return ChatResult(text=text, model="fake", prompt_tokens=0, completion_tokens=0, cached_tokens=0,
                      cost=0.0, latency_s=0.0, finish_reason="stop")


class ScriptedChat:
    """handlers: purpose → 固定字符串、按顺序消费的字符串列表，或接收 messages 的函数。"""

    def __init__(self, handlers: Dict[str, Handler]):
        self.handlers = handlers
        self.calls: List[tuple] = []

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        self.calls.append((purpose, list(messages)))
        handler = self.handlers.get(purpose)
        if handler is None:
            raise AssertionError(f"unexpected model call for purpose {purpose!r}")
        if callable(handler):
            reply = handler(messages)
            return reply if isinstance(reply, ChatResult) else _result(reply)
        if isinstance(handler, list):
            if not handler:
                raise AssertionError(f"no scripted reply left for purpose {purpose!r}")
            return _result(handler.pop(0))
        return _result(handler)

    def purposes(self) -> List[str]:
        return [p for p, _ in self.calls]


_ROUND = re.compile(r"\[Round (\d+)\]\n(?:\[Part [^\n]+\]\n)?User: (.*?)(?=\nAssistant: |\n\n\[Round |\Z)", re.DOTALL)
_SENTENCE = re.compile(r"[^.!?。！？\n]+[.!?。！？]?")
_FIRST_PERSON = re.compile(r"\b(i|i'm|i've|my|me|we|our)\b|我", re.IGNORECASE)
_QUESTION = re.compile(r'The user says: "(.*?)"\n', re.DOTALL)
_FACT_LINE = re.compile(r"^\[(F\d+)\] [^\n]*? · ((?:(?! · ).)+)$", re.MULTILINE)
_EPISODE_USER = re.compile(r"^User: (.+)$", re.MULTILINE)


class HeuristicChat:
    """按 purpose 用规则生成一个格式正确的回复。"""

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        prompt = messages[-1]["content"]
        if purpose == "extract":
            return _result(json.dumps({"facts": self._extract(prompt)}, ensure_ascii=False))
        if purpose == "rerank":
            return _result(json.dumps({"relevant": []}))
        if purpose == "answer_review":
            # Demo substitute only; this is never evidence of answer quality.
            return _result(json.dumps({"responsive": True, "supported": True, "reason": "demo substitute"}))
        if purpose == "query":
            match = _QUESTION.search(prompt)
            question = match.group(1) if match else prompt
            return _result(json.dumps({"queries": [question], "keywords": terms(question)[:8],
                                       "time_range": None, "needs_all": False, "focus": "any"}))
        if purpose == "judge":
            answer = re.search(r"(?:Correct Answer|Explanation|Rubric): (.*?)\n\nModel Response: (.*?)\n\n",
                               prompt, re.DOTALL)
            if answer and answer.group(1).strip().lower() in answer.group(2).lower():
                return _result("yes")
            return _result("no")
        # answer / answer_baseline：挑第一条事实或第一段用户原话当回答
        facts = _FACT_LINE.findall(prompt)
        if facts:
            key, text = facts[-1]
            return _result("NOTES: %s is the most relevant memory.\nANSWER: %s" % (key, text.strip()))
        users = _EPISODE_USER.findall(prompt)
        if users and "Remembered facts" not in prompt and "excerpts" in prompt:
            return _result("NOTES: from a past conversation.\nANSWER: %s" % users[0].strip())
        return _result("NOTES: nothing relevant.\nANSWER: I don't have that information.")

    @staticmethod
    def _extract(prompt: str) -> List[Dict[str, object]]:
        facts = []
        for number, user_text in _ROUND.findall(prompt):
            for sentence in _SENTENCE.findall(user_text):
                sentence = sentence.strip()
                if len(sentence) > 8 and _FIRST_PERSON.search(sentence) and not sentence.endswith("?"):
                    facts.append({"text": sentence, "kind": "personal", "subject": "user", "attribute": "",
                                  "value": sentence, "event_date": None, "rounds": [int(number)],
                                  "importance": 3})
        return facts
