"""本地评分：复刻 LongMemEval 官方 evaluate_qa.py 的评分提示词和判定方式。

正式成绩以项目组用官方脚本 + GPT-4o 统一复跑为准。本地评分可以换成更便宜的评分模型，
用于自己调参和失败分析。输出文件和官方脚本同名同格式（<答案文件>.eval-results-<评分模型名>，
每行带 autoeval_label），report 命令两种都能读。

评分提示词逐字取自 https://github.com/xiaowu0162/LongMemEval/blob/main/src/evaluation/evaluate_qa.py
Copyright (c) 2024 Di Wu, MIT License.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from ..llm import ChatModel, ModelError
from .jsonl import append_jsonl, read_jsonl
from .longmemeval import GoldLabel


class JudgeIncomplete(RuntimeError):
    """有题评分失败（模型调用出错等）。其余题的结果已经写好，重跑同一命令只补失败的题。"""

    def __init__(self, out_path: str, failures: List[Tuple[str, str]]):
        super().__init__("%d 题评分失败，例如 %s：%s" % (len(failures), failures[0][0], failures[0][1]))
        self.out_path = out_path
        self.failures = failures


def _hypotheses(hyp_path: str) -> List[dict]:
    """读答案文件；同一题出现多次时只取第一次（和续跑跳过已答题的口径一致）。"""
    seen, out = set(), []
    for entry in read_jsonl(hyp_path):
        qid = entry.get("question_id")
        if qid is None or "hypothesis" not in entry or qid in seen:
            continue
        seen.add(qid)
        out.append(entry)
    return out

_STANDARD = ("I will give you a question, a correct answer, and a response from a model. Please answer yes if the "
             "response contains the correct answer. Otherwise, answer no. If the response is equivalent to the "
             "correct answer or contains all the intermediate steps to get the correct answer, you should also "
             "answer yes. If the response only contains a subset of the information required by the answer, "
             "answer no. ")
_TAIL = "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."

TEMPLATES = {
    "single-session-user": _STANDARD + _TAIL,
    "single-session-assistant": _STANDARD + _TAIL,
    "multi-session": _STANDARD + _TAIL,
    "temporal-reasoning": _STANDARD + (
        "In addition, do not penalize off-by-one errors for the number of days. If the question asks for the "
        "number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when "
        "the answer is 18), the model's response is still correct. ") + _TAIL,
    "knowledge-update": (
        "I will give you a question, a correct answer, and a response from a model. Please answer yes if the "
        "response contains the correct answer. Otherwise, answer no. If the response contains some previous "
        "information along with an updated answer, the response should be considered as correct as long as the "
        "updated answer is the required answer.") + _TAIL,
    "single-session-preference": (
        "I will give you a question, a rubric for desired personalized response, and a response from a model. "
        "Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does "
        "not need to reflect all the points in the rubric. The response is correct as long as it recalls and "
        "utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}"
        "\n\nIs the model response correct? Answer yes or no only."),
}
ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes "
    "if the model correctly identifies the question as unanswerable. The model could say that the information "
    "is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\n"
    "Explanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? "
    "Answer yes or no only.")


def anscheck_prompt(task: str, question: str, answer: str, response: str, abstention: bool = False) -> str:
    if abstention:
        return ABSTENTION.format(question, answer, response)
    if task not in TEMPLATES:
        raise NotImplementedError(task)
    return TEMPLATES[task].format(question, answer, response)


_NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                 "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
                 "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100, "half": 0.5, "once": 1, "twice": 2}
_ABSTAIN = re.compile(r"\b(don't|do not|doesn't|didn't|did not) (have|know|mention|see|find|recall|contain)|"
                      r"\bno (information|record|mention|details?)\b|not (mentioned|specified|provided|available|"
                      r"enough|in (my|the) memory)|haven't (mentioned|told|shared)|never mentioned|can't (find|tell|"
                      r"determine|say|give|calculate|answer|compute)|cannot (give|calculate|answer|determine|compute)|"
                      r"not enough information|unable to|not sure|没有|不知道|未提及", re.IGNORECASE)
_STOP = {"the", "a", "an", "of", "to", "in", "on", "at", "for", "and", "or", "is", "was", "are", "were", "it",
         "your", "you", "my", "i", "with", "from", "by", "that", "this", "be", "as", "also", "acceptable"}


def _numbers(text: str) -> set:
    found = set()
    for token in re.findall(r"\d+(?:[.,]\d+)*|[A-Za-z]+", text.lower()):
        if token[0].isdigit():
            try:
                found.add(float(token.replace(",", "")))
            except ValueError:
                pass
        elif token in _NUMBER_WORDS:
            found.add(float(_NUMBER_WORDS[token]))
    return found


_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATES = [re.compile(r"^\s*\d+[.)]\s", re.MULTILINE),                              # 列表序号
          re.compile(r"\b%s\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?" % _MONTHS),  # March 7, 2023
          re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?%s(?:,?\s*\d{4})?" % _MONTHS),
          re.compile(r"\b\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?\b"),                       # 2023-03-07
          re.compile(r"\b(?:19|20)\d{2}\b")]                                        # 年份


def _strip_dates(text: str) -> str:
    for pattern in _DATES:
        text = pattern.sub(" ", text)
    return text


def offline_label(gold: GoldLabel, hypothesis: str) -> Optional[bool]:
    """不调用模型的近似评分，只在没有评分模型可用时救急，不是官方口径。

    拒答题看回答是否表示"没有这个信息"；答案里有数字的，看回答里是否出现其中任一数字
    （时间题容许差 1，和官方一致）；否则看答案的实词是否大多出现在回答里。
    偏好题要按评分细则判断，规则做不了，返回 None 表示不评。
    """
    if gold.is_abstention:
        return bool(_ABSTAIN.search(hypothesis))
    if gold.question_type == "single-session-preference":
        return None
    answer, response = gold.answer.lower(), hypothesis.lower()
    opening = re.split(r"(?<=[.!?。:：])\s|\n", response.strip(), 1)[0][:200]
    if _ABSTAIN.search(opening):
        return False            # 可回答的题，开头第一句就说没有信息
    gold_numbers = _numbers(answer)
    if gold_numbers:
        got = _numbers(_strip_dates(response))
        # 官方只对"天数、周数、月数"容许差 1，计数题不容许
        durations = re.search(r"\b(day|week|month|year)s?\b", answer)
        slack = 1.0 if gold.question_type == "temporal-reasoning" and durations else 0.0
        return any(abs(g - h) <= slack for g in gold_numbers for h in got)
    words = [w for w in re.findall(r"[a-z0-9']+", answer) if w not in _STOP]
    if not words:
        return answer.strip() in response
    hits = sum(1 for w in words if re.search(r"\b%s" % re.escape(w[:max(4, len(w) - 2)]), response))
    return hits / len(words) >= 0.6


def judge_offline(hyp_path: str, golds: Dict[str, GoldLabel]) -> str:
    """用 offline_label 给答案文件打分，输出格式与官方脚本相同；偏好题不写入（未评分）。"""
    out_path = "%s.eval-results-offline-rules" % hyp_path
    with open(out_path, "w", encoding="utf-8") as fh:
        for entry in _hypotheses(hyp_path):
            gold = golds.get(entry["question_id"])
            label = offline_label(gold, str(entry["hypothesis"])) if gold else None
            if label is not None:
                fh.write(json.dumps(dict(entry, autoeval_label={"model": "offline-rules", "label": label}),
                                    ensure_ascii=False) + "\n")
    return out_path


def _grade(model, prompt, question_id):
    messages = [{"role": "user", "content": prompt}]
    attempts = []
    for number, limit in enumerate((256, 512), 1):
        reply = model.chat(messages, purpose="judge", max_tokens=limit,
                           tags={"question_id": question_id, "judge_attempt": str(number)})
        text = reply.text.strip()
        label = re.fullmatch(r"(yes|no)[.!]?", text, re.I)
        attempts.append({"text": text, "finish_reason": reply.finish_reason, "model": reply.model})
        if reply.finish_reason in ("content_filter", "refusal"):
            raise ModelError("judge response was blocked: " + reply.finish_reason)
        if reply.finish_reason not in ("length", "incomplete") and label:
            return label.group(1).lower() == "yes", reply.model, attempts
        messages = [{"role": "user", "content": prompt}, {"role": "user", "content":
            "Return one completed label only: yes or no. Do not add notes or explanations."}]
    raise ModelError("judge output was incomplete or not yes/no after one retry")


def judge(hyp_path: str, golds: Dict[str, GoldLabel], model: ChatModel, judge_name: str,
          workers: int = 4, progress: Optional[Callable[[str], None]] = None) -> str:
    """给答案文件打分，结果写到 <hyp_path>.eval-results-<judge_name>；已评过的题跳过。"""
    out_path = "%s.eval-results-%s" % (hyp_path, judge_name)
    done = {r.get("question_id") for r in read_jsonl(out_path) if "autoeval_label" in r}
    todo = [h for h in _hypotheses(hyp_path) if h["question_id"] in golds and h["question_id"] not in done]
    lock = threading.Lock()
    failures: List[Tuple[str, str]] = []

    def one(entry: dict) -> None:
        gold = golds[entry["question_id"]]
        try:
            prompt = anscheck_prompt(gold.question_type, gold.question, gold.answer, str(entry["hypothesis"]),
                                     abstention=gold.is_abstention)
            label, actual_model, attempts = _grade(model, prompt, gold.question_id)
        except Exception as exc:   # 单题失败不影响其他题，最后统一报告
            with lock:
                failures.append((gold.question_id, "%s: %s" % (type(exc).__name__, exc)))
            return
        record = dict(entry, autoeval_label={"model": judge_name, "label": label,
                                           "actual_model": actual_model, "generation_attempts": attempts})
        with lock:
            append_jsonl(out_path, record)
            if progress:
                progress("judged %s: %s" % (gold.question_id, record["autoeval_label"]["label"]))

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        list(pool.map(one, todo))
    if failures:
        raise JudgeIncomplete(out_path, sorted(failures))
    return out_path
