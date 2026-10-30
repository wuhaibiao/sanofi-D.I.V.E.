"""Question operations are separate from retrieval breadth and numeric wording."""
from __future__ import annotations

import re

from .operation_validation import entity_cardinality
from .advice_intent import causal_advice_rules, procedural_advice

OPERATIONS = {"lookup", "latest_state", "historical_state", "compare_states", "sum_events",
              "count_entities", "list", "date_difference", "ratio", "difference", "advice"}
INTERVAL = re.compile(r"how many (?:calendar )?(?:days|weeks|months|years)\b[^?\n]*"
                      r"\b(?:ago|before|after|since|until|between|passed|elapsed|take|taken|been)\b|"
                      r"how long\b[^?\n]*\b(?:ago|before|after|since|until|between)\b|"
                      r"多久以前|多少(?:天|周|个月|年)(?:以前|之前|之后)|"
                      r"(?:相隔|间隔|经过|过去了|用了)(?:多少|几)(?:天|周|个月|年)", re.I)


def is_attendance_day_count(question):
    """Occupied calendar dates differ from elapsed or converted durations."""
    return bool(re.search(r"\bhow many (?:calendar )?days\b", question, re.I)
                and re.search(r"\b(?:attend(?:ed|ing|ance)?|participat(?:ed|ing|ion))\b", question, re.I)
                and not re.search(r"\b(?:elapsed|duration|continuous|hours?|between|before|after|since|until|ago|passed)\b|24[- ]hour|"
                                  r"\b(?:full|whole) days\b", question, re.I))


def is_duration_total(question):
    """Several stated activity durations can be added without calendar endpoints."""
    return bool(re.search(r"\bhow (?:many (?:days|weeks|hours|minutes|seconds)|long)\b"
                          r"[^?\n]*\b(?:take|taken|spend|spent)\b[^?\n]*\b(?:both|and)\b", question, re.I)
                and not re.search(r"\b(?:between|before|after|since|until|ago|elapsed|passed)\b", question, re.I))


def is_calendar_span(question):
    """A from/to month or year span, rather than a sum of separate activities."""
    return bool(re.search(r"\bhow many (?:calendar )?(?:months|years)\b[^?\n]*\bfrom\b[^?\n]+\bto\b", question, re.I)
                and not re.search(r"\b(?:both|and|combined|excluding|gaps|breaks)\b", question, re.I))


def operation_for(question):
    """Conservative fallback. Numeric questions are not automatically sums."""
    if re.search(r"\b(percentage of|percent of|what percentage|what percent|ratio of|proportion of)\b|占.*(?:百分之|比例)|百分比|比率", question, re.I):
        return "ratio"
    if is_duration_total(question):
        return "sum_events"
    if is_calendar_span(question):
        return "date_difference"
    if INTERVAL.search(question) and not re.search(r"\b(total|sum|across)\b|总共|合计", question, re.I):
        return "date_difference"
    if (re.search(r"\bhow (?:much|many)\b", question, re.I) and
            re.search(r"\b(?:left|remaining)\b|\bneed to (?:earn|save|read|collect|accumulate)\b", question, re.I)
            or re.search(r"还(?:需要|要|差|剩).*多少|还差几个", question)):
        return "difference"
    if (re.search(r"\bhow (?:much|many)\b", question, re.I) and
            re.search(r"\b(?:more|less|save|saved|savings|increase|decrease|older|younger|extra|difference)\b", question, re.I)
            or re.search(r"\b(?:calculate|compute)\b[^?\n]*\bdifference\b|差(?:多少|几)|省了多少|多了多少|少了多少", question, re.I)):
        return "difference"
    if (re.search(r"\b(?:what|which|where|when)\b", question, re.I) and
            re.search(r"\b(?:previous(?:ly)?|former|prior)\b|此前|之前的|上一个", question, re.I) and
            not re.search(r"\b(?:now|currently|current|today|most recent|latest|compare)\b|现在|目前|最新", question, re.I)):
        return "historical_state"
    if re.search(r"\b(most recent(?:ly)?|latest|personal best)\b|最近一次|最新|个人最佳", question, re.I):
        return "latest_state"
    now = bool(re.search(r"\b(now|currently|current|today)\b|现在|目前", question, re.I))
    old = bool(re.search(r"\b(initially|originally|started|previous|before|used to)\b|最初|以前|原来", question, re.I))
    if (now and old) or re.search(r"\b(compare|changed from|then and now)\b|前后对比|变化", question, re.I):
        return "compare_states"
    if re.search(r"\b(so far|latest|cumulative|need to reach|needed to reach)\b|目前累计|读到|进度", question, re.I):
        return "latest_state"
    if re.search(r"\b(total|sum|add up|altogether|across all)\b|总共|合计|一共|求和", question, re.I):
        return "sum_events"
    if re.search(r"\b(how many times|how often|count|number of .* attended)\b|几次|多少次", question, re.I):
        return "count_entities"
    if entity_cardinality(question):
        return "count_entities"
    if re.search(r"\bhow many\b|多少[个件本条只]|几个", question, re.I) and not re.search(
            r"\b(?:hours?|days?|weeks?|months?|years?|minutes?|pages?)\b|多少[天周年月小时页]", question, re.I):
        return "count_entities"
    if re.search(r"\b(list|all the|every)\b|列出|所有", question, re.I) and not re.search(
            r"\b\d+(?:st|nd|rd|th)\b|第\s*\d+", question, re.I):
        return "list"
    if re.search(r"\b(suggest(?:ions?)?|recommend(?:ations?)?|ideas?|advice|should I|any tips)\b|建议|推荐|"
                 r"\bdo you think (?:it|this|that) (?:might|could|may)\b|"
                 r"\bwhat (?:might|could) be causing\b", question, re.I) or procedural_advice(question):
        return "advice"
    if now or re.search(r"\b(how many .* (?:have|own|are there)|what time .* (?:wake|start|finish))\b|有多少|几点", question, re.I):
        return "latest_state"
    return "lookup"


def is_aggregate(question, operation=None):
    return (operation or operation_for(question)) in ("sum_events", "count_entities")


def complete_search(operation):
    return operation in ("sum_events", "count_entities", "compare_states", "latest_state", "historical_state", "list", "ratio", "difference")


def collection_rules(operation, qualifiers=()):
    """Page collection cannot resolve relationships whose evidence is elsewhere."""
    return ("Question operation: %s. This is evidence collection, before global synthesis. "
            "Keep a source when it may supply ANY requested object, operand, state, date, event or "
            "identity link. A single record need not contain every requested condition or both sides "
            "of a comparison. Retain earlier and later states; do not select the final applicable "
            "state or decide an inter-record before/after relationship using this page alone. "
            "Preserve literal source context so global synthesis can apply all constraints without "
            "transferring values between unrelated objects.%s" % (operation,
            (" Whole-question constraints to resolve across records: " + "; ".join(qualifiers) + ".") if qualifiers else ""))


def operation_rules(operation, qualifiers=(), question=''):
    descriptions = {
        "lookup": "Retrieve the requested value or exact original passage; do not add unrelated numeric mentions.",
        "latest_state": "Select the latest applicable state for the SAME object and conditions. A cumulative snapshot includes earlier progress: never add its predecessors. Apply only explicit later increments not already included. A generic routine cannot override a newer conditional routine. Do not invent a later event date from an undated 'recent' mention. If event order is unresolved, state which applicable event the user most recently described instead of claiming a proved occurrence order.",
        "historical_state": "Select the state valid at the requested historical time, retaining replaced versions as evidence. For 'previous' without a date, identify the current state first and return the state immediately preceding its last change. Recalling the current value in past-tense words does not establish another earlier value.",
        "compare_states": "Answer each requested time/state separately. Preserve both values and their labels; never sum them.",
        "sum_events": "Sum only qualifying independent events, or the latest cumulative snapshot per distinct object. Resolve snapshots and repeated descriptions globally before arithmetic. Do not add a snapshot to increments already included in it.",
        "count_entities": "Count distinct qualifying objects or occurrences. An explicit cumulative count for the SAME requested scope can establish the count even when individual names are absent. Select its latest applicable snapshot; never add earlier snapshots or already-included objects. Preserve context resolving pronouns such as 'ones'. Verify ownership, container and all requested conditions before counting. Repeated descriptions are not new objects.",
        "list": "Enumerate all qualifying entries, preserving original list identity, order and speaker.",
        "date_difference": "Use source-backed dates or explicit relative relations between the requested actions, anchored to the caller clock when required.",
        "ratio": "Retrieve BOTH the numerator and denominator for the requested objects, including estimates or plans when asked about planned costs. Compute numerator / denominator, multiplied by 100 for a percentage. A small relevance score for one operand does not make it irrelevant. A directly quoted ratio or multiplier can establish the requested relative value without knowing its absolute prices. Do not substitute another object's value.",
        "difference": "Retrieve BOTH the baseline and alternative quantities for the requested objects and time. Subtract in the order asked: savings are baseline cost minus alternative cost; an amount remaining to earn, save or complete is the target minus the latest applicable balance or progress. A stated total requirement is not the amount still needed. The sources need not explicitly state the computed difference. Preserve each quantity's object, speaker and uncertainty; assistant examples or generic estimates alone cannot establish what the user paid, reported or planned. If a personal comparison lacks a user-reported operand, identify it as missing instead of filling it from general assistant advice. Assistant evidence applies when explicitly recalling an assistant's quoted comparison. Do not substitute another scope or combine incompatible units.",
        "advice": "Give suggestions tied to the user's own recalled interests and constraints. Honor their specific format, platform, access, budget, exclusions and circumstances before choosing an option. A broad current request does not revoke a narrower recorded preference. Prefer options that meet all applicable constraints; do not substitute a related genre or platform merely because it shares a topic. Check previous use/viewing and recent changes. A prior assistant suggestion is not proof the user tried it and does not require choosing something different; avoid repeats only when the user's viewing/use or request supports that exclusion. For a current problem, consider recalled changes as possible factors instead of assuming an earlier suggestion solved it. Explain the relevant remembered details briefly, distinguish unconfirmed plans from completed actions, and keep speculation conditional.",
    }
    base="Question operation: %s. %s%s" % (operation, descriptions.get(operation, descriptions["lookup"]),
        (" Required qualifiers: " + "; ".join(qualifiers) + ". Do not transfer values from a different object or scope.") if qualifiers else "")
    causal=causal_advice_rules(question,operation)
    return base+('\n'+causal if causal else '')
