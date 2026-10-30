"""自拟原始会话的记忆机制回归：真实写入、更新、检索、回答，不是 Oracle 成绩。"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Sequence, Tuple

from ..config import MemorySettings, Models
from ..core import MemorySystem
from ..llm import ModelError
from .provenance import fingerprint, model_manifest, prompt_fingerprint, source_manifest


@dataclass(frozen=True)
class MechanismCase:
    name: str
    sessions: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]
    question: str
    expected: str
    today: str = "2025-05-04T10:00"
    forget_text: str = ""


def session(day: str, user: str, assistant: str = ""):
    turns = (("user", user),) + ((("assistant", assistant),) if assistant else ())
    return (day + "T10:00", turns)


# 检查答案只在 respond 返回后使用；记忆系统只收到原始会话、日期、问题和显式遗忘操作。
CASES = (
    MechanismCase("independent_events", (
        session("2025-04-05", "I spent 14 minutes in a robotics practice session today."),
        session("2025-04-12", "I spent 17 minutes in another robotics practice session today.")),
        "Add my time in the two robotics sessions. Reply with only the total number of minutes.", "31"),
    MechanismCase("cumulative_progress", (
        session("2025-04-05", "My cumulative study log has reached 6 hours."),
        session("2025-04-12", "My cumulative study log is now 11 hours, including the earlier 6.")),
        "What is my latest cumulative study time? Reply with only the number of hours.", "11"),
    MechanismCase("state_update", (
        session("2025-03-05", "I live in Quito."),
        session("2025-04-12", "I have moved to Cuenca and live there now.")),
        "Where do I live now? Reply with only the city name.", "Cuenca"),
    MechanismCase("tentative_update", (
        session("2025-03-05", "I live in Quito."),
        session("2025-04-12", "I might move to Cuenca, but I have not decided and still live in Quito.")),
        "Where do I currently live? Reply with only the city name.", "Quito"),
    MechanismCase("historical_version", (
        session("2025-03-05", "My desk is made of cedar."),
        session("2025-04-12", "I replaced that desk with a maple desk.")),
        "What wood was my desk made of before I replaced it? Reply with only the wood name.", "cedar"),
    MechanismCase("prepaid_future_event", (
        session("2025-04-05", "I paid $120 today for a drawing retreat on June 20."),
        session("2025-04-12", "I paid $45 today for a pottery retreat on July 9.")),
        "How much have I already paid for these retreats? Reply with only the total number of dollars.", "165"),
    MechanismCase("recommendation_scope", (
        session("2025-04-05", "What organizers do you recommend for my art portfolio?",
                "For your portfolio, I recommend a ring binder and tab dividers, in that order. "
                "For course notes, spiral notebooks are another option."),),
        "Name the two organizers recommended for my art portfolio in their original order. "
        "Reply with just the names separated by a comma.", "ring binder, tab dividers"),
    MechanismCase("ownership_vs_suggestion", (
        session("2025-04-05", "My only weather instrument is a thermometer.",
                "You could also consider a hygrometer and a rain gauge."),),
        "How many weather instruments have I said I own? Reply with only the number.", "1"),
    MechanismCase("booking_relative_to_today", (
        session("2025-01-10", "I reserved my cabin on November 10, 2024 for a trip on March 10, 2025."),),
        "How many calendar months ago did I reserve that cabin? Reply with only the number.", "3",
        today="2025-02-10T10:00"),
    MechanismCase("month_precision", (
        session("2025-05-01", "I bought my sketchbook in April 2025. I don't recall the exact day."),),
        "What exact day of April did I buy the sketchbook? Reply with only the day number "
        "or unknown if no exact day is known.", "unknown"),
    MechanismCase("entity_alias", (
        session("2025-04-05", "Dr. Inez Salcedo joined my workshop today."),
        session("2025-04-12", "Inez from last week's workshop is Dr. Inez Salcedo; nobody else joined.")),
        "How many distinct people have I said joined that workshop? Reply with only the number.", "1"),
    MechanismCase("shared_source_forgetting", (
        session("2025-04-05", "I live in Quito and collect amber fossils."),),
        "Where do I live? Reply with only the city name or unknown if it is unavailable.", "unknown",
        forget_text="Quito"),
)


def run_mechanisms(models: Models, settings: MemorySettings, out: str,
                   cases: Sequence[MechanismCase] = CASES) -> Dict[str, Any]:
    if models.chat is None or models.for_purpose("extract") is None:
        raise ModelError("mechanism regression needs both a memory model and an answer model")
    rows = []
    blocked = False
    for case in cases:
        system = MemorySystem(models, settings, trace_dir=out + "/traces")
        row = {"case": case.name, "expected": case.expected, "question": case.question}
        started = time.monotonic()
        stage = "write"
        try:
            for at, turns in sorted(case.sessions):
                when = datetime.fromisoformat(at)
                handle = system.open_session(case.name, when)
                for role, text in turns:
                    system.add_turn(handle, role, text, when)
                system.close_session(handle, when, tags={"mechanism_case": case.name})
            row["write_stats"] = system.stats(case.name)
            if case.forget_text:
                stage = "forget"
                targets = [f.key for f in system.facts(case.name) if case.forget_text.casefold() in f.text.casefold()]
                if not targets:
                    raise ModelError("no extracted fact matched the explicit forget request")
                row["forgotten"] = [key for target in targets for key in system.forget(case.name, target)]
            stage = "answer"
            reply = system.respond(case.name, case.question, datetime.fromisoformat(case.today),
                                   tags={"mechanism_case": case.name})
            row.update(answer=reply.text, passed=reply.text.strip().casefold() == case.expected.strip().casefold(),
                       trace=reply.trace, pending_extraction=system.stats(case.name)["backlog"])
            if row["pending_extraction"]:
                row["passed"] = False
                row["failure_reason"] = "extraction did not complete"
        except ModelError as exc:
            row.update(error=str(exc), stage=stage, passed=None)
            blocked = exc.account_problem
        finally:
            system.close()
        row["latency_s"] = round(time.monotonic() - started, 3)
        rows.append(row)
        if blocked:
            break
    return {"mode": "self-authored end-to-end memory mechanism regression; exact answers; not a benchmark score",
            "status": "blocked" if blocked else "complete", "total": len(cases),
            "answered": sum("error" not in r for r in rows), "passed": sum(r["passed"] is True for r in rows),
            "incorrect": sum(r["passed"] is False for r in rows), "errors": sum("error" in r for r in rows),
            "unstarted": len(cases) - len(rows), "provenance": source_manifest(),
            "prompt_fingerprint": prompt_fingerprint(), "models": model_manifest(models),
            "settings": settings.to_dict(), "cases_fingerprint": fingerprint([asdict(c) for c in cases]),
            "results": rows}
