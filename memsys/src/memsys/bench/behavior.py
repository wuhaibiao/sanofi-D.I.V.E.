"""自拟的回答行为回归：固定记忆检查生成策略，不替代 LongMemEval 评测。"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Sequence

from .. import prompts
from ..aggregation import answer_with_validation, is_temporal_interval, wants_calculation
from ..llm import ChatModel, ModelError
from .provenance import fingerprint, model_details, prompt_fingerprint, source_manifest


@dataclass(frozen=True)
class BehaviorCase:
    name: str
    memory: str
    question: str
    expected: str
    today: str = "2025-05-04"


# 全部为自拟内容；expected 只用于回答后检查，绝不放入模型请求。
CASES = (
    BehaviorCase("independent_events",
                 "[F1] happened 2025-04-05 · The user spent 14 minutes in a robotics practice session.\n"
                 "[F2] happened 2025-04-12 · The user spent 17 minutes in a second robotics practice session.",
                 "Add the time I spent in the two robotics sessions. Reply with only the total number of minutes.", "31"),
    BehaviorCase("cumulative_progress",
                 "[F1] happened 2025-04-05 · replaced by F2 · The user's cumulative study log was 6 hours.\n"
                 "[F2] happened 2025-04-12 · The user's cumulative study log is now 11 hours, including the earlier 6.",
                 "What is my latest cumulative study time? Reply with only the number of hours.", "11"),
    BehaviorCase("group_quantities",
                 "[F1] happened 2025-04-05 · Three apprentices joined the user's lab.\n"
                 "[F2] happened 2025-04-12 · One supervisor joined the same lab.",
                 "How many people joined my lab across these two occasions? Reply with only the number.", "4"),
    BehaviorCase("prepaid_future_event",
                 "[E1] conversation on 2025-04-05\nUser: I paid $120 today for a drawing retreat on June 20.\n"
                 "[E2] conversation on 2025-04-12\nUser: I paid $45 today for a pottery retreat on July 9.",
                 "How much have I already paid for these retreats? Reply with only the total number of dollars.", "165"),
    BehaviorCase("recommendation_scope",
                 "[F1] noted 2025-04-05 · The assistant mentioned spiral notebooks for course notes.\n"
                 "[E1] conversation on 2025-04-05\nUser: Which organizers do you recommend for my art portfolio?\n"
                 "Assistant: For the portfolio, I recommend a ring binder and tab dividers. "
                 "For course notes, spiral notebooks are another option.",
                 "Name the two organizers you recommended for my art portfolio, in their original order. "
                 "Reply with just the names separated by a comma.", "ring binder, tab dividers"),
    BehaviorCase("ownership_vs_suggestion",
                 "[E1] conversation on 2025-04-05\nUser: My only weather instrument is a thermometer.\n"
                 "Assistant: You could also consider a hygrometer and a rain gauge.",
                 "How many weather instruments have I said I own? Reply with only the number.", "1"),
    BehaviorCase("booking_relative_to_today",
                 "[E1] conversation on 2025-01-10\nUser: I reserved my cabin on November 10, 2024 "
                 "for a trip on March 10, 2025.",
                 "How many calendar months ago did I reserve that cabin? Reply with only the number.",
                 "3", today="2025-02-10"),
    BehaviorCase("month_precision",
                 "[F1] happened 2025-04 · The user bought a sketchbook in April 2025. "
                 "The exact purchase day was not recorded.",
                 "What exact day of April did I buy the sketchbook? Reply with only the day number, "
                 "or unknown if the day was not recorded.", "unknown"),
    BehaviorCase("historical_version",
                 "[F1] happened 2025-03-05 · replaced by F2 · The user's desk was made of cedar.\n"
                 "[F2] happened 2025-04-12 · The user replaced that desk with a maple desk.",
                 "What wood was my desk made of before I replaced it? Reply with only the wood name.", "cedar"),
    BehaviorCase("direct_relationship",
                 "[F1] noted 2025-04-05 · The user often visits Northwick Supply.\n"
                 "[E1] conversation on 2025-04-12\nUser: Although I usually visit Northwick Supply, "
                 "I bought the replacement cables at Westhaven Depot yesterday.",
                 "Which shop sold me the replacement cables? Reply with only the shop name.", "Westhaven Depot"),
)


def run_behavior(model: ChatModel, cases: Sequence[BehaviorCase] = CASES) -> Dict[str, Any]:
    run_id = datetime.utcnow().isoformat(timespec="microseconds") + "Z"
    rows = []
    blocked = False
    for case in cases:
        user_prompt = prompts.ANSWER_QA.format(today=case.today, memory=case.memory,
                                              question=case.question, rules=prompts.ANSWER_RULES)
        if is_temporal_interval(case.question):
            user_prompt += "\n\n" + prompts.DATE_CALCULATION_RULES
        elif wants_calculation(case.question):
            user_prompt += "\n\n" + prompts.calculation_rules(case.question)
        markers = list(re.finditer(r"(?m)^\[(F\d+|E\d+)\]", case.memory))
        evidence = {m.group(1): case.memory[m.end():markers[n + 1].start() if n + 1 < len(markers) else len(case.memory)]
                    for n, m in enumerate(markers)}
        started = time.monotonic()
        row = {"case": case.name, "expected": case.expected, "question": case.question,
               "memory": case.memory, "today": case.today}
        try:
            notes, answer, audit = answer_with_validation(model,
                [{"role": "system", "content": prompts.ANSWER_SYSTEM},
                 {"role": "user", "content": user_prompt}],
                evidence=evidence, max_tokens=1024, tags={"behavior_case": case.name, "run_id": run_id},
                question=case.question, today=case.today)
            row.update({"notes": notes, "answer": answer,
                        "aggregation": audit,
                        "passed": answer.strip().casefold() == case.expected.strip().casefold()})
        except ModelError as exc:
            row.update({"error": str(exc), "passed": None})
            blocked = exc.account_problem
        row["latency_s"] = round(time.monotonic() - started, 3)
        rows.append(row)
        if blocked:
            break
    return {"mode": "answer-only; self-authored synthetic memories; exact requested output; not a benchmark score",
            "run_id": run_id, "status": "blocked" if blocked else "complete",
            "total": len(cases), "answered": sum("error" not in r for r in rows),
            "passed": sum(r["passed"] is True for r in rows),
            "incorrect": sum(r["passed"] is False for r in rows),
            "errors": sum("error" in r for r in rows),
            "failed": sum(r["passed"] is not True for r in rows), "unstarted": len(cases) - len(rows),
            "model": model_details(model), "provenance": source_manifest(),
            "prompt_fingerprint": prompt_fingerprint(),
            "cases_fingerprint": fingerprint([asdict(c) for c in cases]), "results": rows}
