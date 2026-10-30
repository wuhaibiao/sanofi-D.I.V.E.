"""Bounded cross-page interpretation over already verified source excerpts.

Models decide relevance and event identity. Programs check references, coverage,
snapshot uniqueness, timestamps and arithmetic; these checks are not a proof of
semantic correctness. No benchmark labels enter these requests.
"""
from __future__ import annotations

import json
import re

from .llm import IncompleteJSONError, chat_json
from .queryops import collection_rules, operation_rules
from .user_context import literal_user_index
from .quantity_scope import relative_quantity_rules

RECONCILE_RULES = """Resolve the supplied numeric evidence globally before calculation. Return JSON only:
{"groups":[{"kind":"independent"|"duplicate"|"cumulative","scope":"object/activity being measured","members":["C1"],"keep":["C1"],"reason":"why these records describe this scope and event"}],"excluded":["C2"],"insufficient":false,"reason":""}.
Account for EVERY record exactly once in a group or excluded. Keep only records that satisfy the question's object, ownership, action, time and quantity scope. A question's premise does not establish ownership or a container.
Independent records are separate occurrences: keep all members. Duplicate records describe the SAME occurrence: keep one with the same quantity. Cumulative records are snapshots for the SAME measured object: keep only the latest snapshot. Never add older snapshots to the latest, or add already-included increments. Distinct objects may each contribute their latest cumulative snapshot. Give cumulative groups different scopes only when they truly concern different objects.
Distinguish requested totals from available totals: overall servings, dinners, planned quantities and group counts need not be the requested subset. If a quote does not establish the relevant quantity, exclude it. Do not use suggestions as actions the user completed. Preserve speaker attribution: assistant examples, generic prices and estimates alone do not establish the user's personal quantity. Mixed or ambiguous quotes require reading their role labels. Do not invent, edit or convert values. If the records cannot establish the requested scope or completeness, set insufficient=true instead of constructing a plausible total."""

SELECTION_RULES = """Select source excerpts sufficient to answer the question. Return JSON only:
{"keep":["C1"],"excerpts":[{"id":"C1","quote":"shorter exact contiguous excerpt of that record"}],"insufficient":false,"reason":"brief selection rationale"}.
For computed differences, ratios or totals, source operands can establish the answer even when no source states the computed result. Retain each applicable operand and its object/identity context. A personal quantity needs user evidence: an assistant example or generic estimate does not establish what the user paid, reported or planned. A user-reported estimate is valid as an estimate even when the assistant disputed it. Speaker metadata describes the literal excerpt; mixed or ambiguous excerpts require reading role labels.
Keep earlier identity context when later updates refer to the earlier state rather than naming its object. For cumulative progress retain the latest snapshot and only explicit subsequent increments not included in it.
Use only the provided record IDs. The excerpts field is optional: it shortens each retained record to an EXACT contiguous substring of its supplied quote. Never paraphrase. Preserve ALL evidence needed for the requested operation, objects and conditions, but trim irrelevant surrounding advice to fit the stated content budget. Retain the earlier identity statement when a later amount uses a pronoun. For latest_state choose the newest applicable state for the SAME scope, retaining context establishing ownership or qualifiers. A newer conditional routine overrides an older generic routine for those conditions only. For compare_states keep evidence for each requested state and label. Cumulative progress is a snapshot, not an increment. For historical_state choose evidence valid at that time. For lists keep every requested entry; do not discard needed entries merely to fit a budget. For dates keep both actions and any explicit relative relation between them. Never answer the question here or invent an excerpt. A questioned premise is not an established fact. If the evidence is absent, mark insufficient rather than substitute another object."""

LOCAL_SELECTION_RULES = """Compact this batch of original source excerpts before global selection. Return JSON only:
{"reviewed":["C1"],"keep":["C1"],"excerpts":[{"id":"C1","quote":"exact contiguous substring"}],"insufficient":false,"reason":"brief rationale"}.
reviewed must include EVERY supplied record exactly once. Keep any evidence that may supply a requested object, operand, state, date or identity link. Other batches may establish its relevance and relationships. Do not choose the latest state or require both sides of a comparison in this batch. Preserve personal user statements, labels identifying amounts and context identifying pronouns; remove unrelated surrounding advice. Quotes must be exact contiguous substrings. Never paraphrase, answer, invent records or silently discard required list entries to fit. The content budget includes each retained source header. Set insufficient=true only if necessary evidence cannot be retained within that budget. A missing operand or identity link on this batch does not establish global insufficiency. An empty keep is allowed for an irrelevant batch. Global synthesis will assess whether the combined sources are sufficient."""

RESPONSE_RULES = """Check a proposed personal-assistant answer against the original question and the supplied memory. Return JSON only:
{"responsive":true,"supported":true,"reason":"brief reason"}.
For advice, explicitly connect the explanation to the user's applicable specific format, platform, access, budget, exclusions or recalled circumstances. Generic category tips that omit this connection are not responsive. A broader request does not revoke constraints. Assistant suggestions do not prove use or require novelty. Never invent changes from assumptions or suggestions.
Recording-date lower bounds apply only to requested minimum day/week intervals. Confirm already completed original-user wording, not plans, negations, hypotheticals or assistant advice; preserve at least and unknown exact interval. In ordinary month/year questions, just-completed wording may support an approximate near-record-date interval. It does not establish an exact day, and plain undated past actions do not establish recency.
For casual item/name/brand recall, a uniquely known description from a named seller answers with that precise relationship. Do not negate this useful identification with an unnecessary brand-unrecorded disclaimer; manufacturer-specific and absent numeric/property questions still need honest qualification.
Responsive means it actually answers every requested part or correctly explains missing information; an unrelated token is not an answer. Supported means recalled personal facts, quantities, objects, ownership, current/historical states and conditions agree with the evidence. Original user statements take precedence over derived summaries and unresolved update labels. Assistant examples or generic estimates alone do not establish user prices, payments or plans. A user-reported estimate must remain approximate and must not be replaced by an assistant counterclaim. Computed differences or ratios are supported by the applicable source operands; the result need not be quoted verbatim. An unconfirmed update relationship means the memory resolver could not link versions; it does not negate a clear original user statement. For the same requested property, prefer the most recent explicit user statement unless the question specifies an earlier time. A later recollection can correct an earlier value; past-tense wording alone does not request the earliest recorded value. Within one session on the same date, later rounds can update earlier statements. A newer approximate estimate can update an older exact count: preserve its approximation rather than reverting to the old count. Distinguish genuine plans, questions and uncertainty in the original words from bookkeeping uncertainty. One week is exactly seven days; complete dated endpoints establish elapsed days. Program-verified arithmetic does not prove event qualification or completeness, which you must still assess. Ownership established on an earlier source date can establish that acquisition preceded a later event, without establishing the exact acquisition date. Ordinary new advice can use general knowledge but must respect recalled preferences and constraints. Do not require newly suggested recipes or ideas to occur in past memory. Do not accept a total in place of a state or comparison, or facts from a different object/container. If the question's terminology differs from the original passage, an answer can quote what was originally given and explain the terminology. A qualified recall can give a unique related record's known detail with its original wording and clearly state which requested label or condition is unconfirmed. Such a qualification must not affirm a conflicting premise, transfer quantities between distinct objects or give a substituted number for a missing qualifying object. Distinguish latest mentioned events from proved occurrence order, and anchor quoted relative times to their source date. A refusal is supported only when the requested information is actually absent or unresolved in the supplied sources; check all relevant source passages before accepting a refusal."""


RESPONSE_RULES += """ Check linked turns together: a routine's frequency and its place or weekday may be supplied in different statements about the same activity. A missing detail in one excerpt is not absent from the complete evidence. Do not move qualifiers between unrelated activities or time periods. An amount still needed is the target minus the latest balance, not the target itself. For any ordinal_progress claim, verify completed cumulative progress rather than rank, preference, list position or future plans. For a source-year date anchor, verify that the omitted year belongs to that recorded year with no conflicting temporal context. Program-verified nearest calendar units are supported when explicitly approximate and exact or complete units were not requested. For item identification, the precise original description and seller relationship can be responsive and supported even when the question casually uses another identity label. Describing an item as from its named seller does not assert its manufacturer. Do not require a blanket refusal for that wording; a refusal is not responsive when it withholds a uniquely identified item's available description."""

RESPONSE_RULES += """ A program-verified calendar-year label difference can support an explicitly approximate year span when the source years qualify both requested endpoints. It cannot prove exact elapsed days or full years, or continuous activity across an explicit gap. Advice should respect previous trials and recent changes; an old assistant suggestion is not proof that it worked or that the user tried it."""

DATE_RESPONSE_RULES = """ For ordinary first/last/latest event recall, a source-backed interval explicitly scoped to the most recent matching occurrence in the available dated records can be responsive without claiming real-world finality or absence of unrecorded events. The word last alone does not require proving that no unrecorded event ever occurred. Do not reject that clearly stated record scope solely because the source does not use the word last. Verify ordering by established occurrence dates, not note timestamps. Literal today/yesterday/tomorrow plus the complete source recording day establishes an exact event day through the validated relative endpoint schema. A newer undated matching event, a specifically requested actual final/ever occurrence, or a missing endpoint still requires an explicit limitation; do not affirm real-world finality, transfer an unrelated action's date, or fabricate missing dates."""


def _request(model, rules, content, purpose, settings, tags=None, max_tokens=None):
    from .budget import budget_counter
    messages = [{"role": "system", "content": rules}, {"role": "user", "content": content}]
    count = budget_counter(settings).messages(messages)
    if count > settings.answer_input_budget_tokens:
        raise ValueError(purpose + " exceeds full prompt budget")
    data = chat_json(model, messages, purpose=purpose,
                     max_tokens=max_tokens or settings.extract_max_tokens, tags=tags)
    return data, count


def _ids(value, allowed):
    if not isinstance(value, list) or any(not isinstance(i, str) or i not in allowed for i in value):
        raise ValueError("semantic review referenced an unavailable record")
    if len(set(value)) != len(value):
        raise ValueError("semantic review repeated a record")
    return value


def _literal_qualifiers(question, qualifiers):
    # Literal size/unit qualifiers can be checked without inferring ownership.
    # Other semantic conditions remain explicitly visible to the model review.
    return [m.group(0) for m in re.finditer(r"\b\d+(?:\.\d+)?[- ](?:gallons?|liters?|litres?|bedrooms?|acres?)\b", question, re.I)]


def _normalized(text):
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def reconcile(model, question, items, settings, today, tags=None, operation="sum_events", qualifiers=()):
    from .aggregation import _quantity_decimal
    records = [{"id": "C%d" % (n + 1), **item} for n, item in enumerate(items)]
    content = "Today: %s\nQuestion: %s\n%s\nRecords:\n%s" % (
        today, question, operation_rules(operation, qualifiers), json.dumps(records, ensure_ascii=False))
    data, count = _request(model, RECONCILE_RULES, content, "aggregate_reconcile", settings, tags)
    if not isinstance(data, dict) or not isinstance(data.get("insufficient"), bool):
        raise ValueError("semantic reconciliation needs an explicit sufficiency decision")
    audit = {"prompt_count": count, "decision": data,
             "scope": "model event identity and qualification; record partition, cumulative recency and literal numeric qualifiers checked by program"}
    if data["insufficient"]:
        raise ValueError("semantic evidence insufficient: " + str(data.get("reason", "")))
    by_id = {r["id"]: r for r in records}
    if not isinstance(data.get("groups"), list):
        raise ValueError("semantic reconciliation groups are missing")
    reviewed = list(_ids(data.get("excluded"), by_id))
    chosen, cumulative_scopes = [], set()
    for group in data["groups"]:
        if not isinstance(group, dict) or group.get("kind") not in ("independent", "duplicate", "cumulative"):
            raise ValueError("semantic reconciliation group is invalid")
        members = _ids(group.get("members"), by_id)
        keep = _ids(group.get("keep"), members)
        if not members or not keep:
            raise ValueError("a contribution group cannot be empty")
        reviewed.extend(members)
        kind = group["kind"]
        if kind == "independent" and any('value' in by_id[i] and
                by_id[i].get("value_type") in ("cumulative", "state") for i in members):
            raise ValueError("state or cumulative evidence cannot be labelled an independent increment")
        if kind == "independent" and set(keep) != set(members):
            raise ValueError("independent groups must keep every member")
        if kind in ("duplicate", "cumulative") and len(keep) != 1:
            raise ValueError("duplicate or cumulative groups must keep one record")
        if kind == "duplicate" and len({_quantity_decimal(by_id[i]["value"]) if "value" in by_id[i] else None
                                        for i in members}) > 1:
            raise ValueError("duplicate records disagree on quantity; resolve the state or conflict")
        if kind == "cumulative":
            scope = _normalized(str(group.get("scope") or ""))
            if not scope or scope in cumulative_scopes:
                raise ValueError("one latest cumulative snapshot is required per scope")
            cumulative_scopes.add(scope)
            dates = {i: by_id[i].get("recorded_at", "") for i in members}
            if dates[keep[0]] != max(dates.values()):
                raise ValueError("selected cumulative snapshot is older than another supplied snapshot")
        chosen.extend(keep)
    if len(reviewed) != len(set(reviewed)) or set(reviewed) != set(by_id):
        raise ValueError("semantic reconciliation did not partition every record exactly once")
    selected = [items[int(i[1:]) - 1] for i in chosen]
    text = " ".join(i["quote"] for i in selected)
    for qualifier in _literal_qualifiers(question, qualifiers):
        if _normalized(qualifier) not in _normalized(text):
            raise ValueError("requested object qualifier has no selected source evidence: " + qualifier)
    if not selected:
        raise ValueError("no contributions qualify for the requested scope")
    audit.update(records=records, retained=chosen)
    return selected, audit


def _selection_content(question, records, settings, today, operation, qualifiers, content_budget, local=False):
    return "Today: %s\nQuestion: %s\n%s\nContent budget including source headers: %s %s units.\nRecords:\n%s" % (
        today, question, (collection_rules if local else operation_rules)(operation, qualifiers),
        content_budget, settings.budget_mode, json.dumps(records, ensure_ascii=False))


def _select_once(model, question, excerpts, settings, today, tags, operation, qualifiers, content_budget, local=False):
    records = [{"id": "C%d" % (n + 1), "source": header, "quote": quote}
               for n, (header, quote) in enumerate(excerpts)]
    data, count = _request(model, LOCAL_SELECTION_RULES if local else SELECTION_RULES,
        _selection_content(question, records, settings, today, operation, qualifiers, content_budget, local),
        "evidence_compact" if local else "evidence_select", settings, tags)
    if not isinstance(data, dict) or not isinstance(data.get("insufficient"), bool):
        raise ValueError("evidence selection needs an explicit sufficiency decision")
    allowed = {r["id"] for r in records}
    if local and set(_ids(data.get("reviewed"), allowed)) != allowed:
        raise ValueError("evidence compaction did not review every supplied record")
    if data["insufficient"]:
        # A batch is not asked to answer the question. Its insufficiency flag
        # means it cannot preserve needed evidence within the allocated space.
        raise ValueError("semantic evidence insufficient: " + str(data.get("reason", "")))
    ids = _ids(data.get("keep"), allowed)
    if not ids and not local:
        raise ValueError("no sufficient source excerpts were selected")
    shortened = {}
    if "excerpts" in data:
        if not isinstance(data["excerpts"], list):
            raise ValueError("compacted excerpts must be a list")
        by_id = {r["id"]: r for r in records}
        for item in data["excerpts"]:
            if not isinstance(item, dict) or item.get("id") not in ids or item["id"] in shortened:
                raise ValueError("compacted excerpt references an unavailable or repeated record")
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote.strip() or quote not in by_id[item["id"]]["quote"]:
                raise ValueError("compacted quote must be an exact contiguous source excerpt")
            shortened[item["id"]] = quote
    selected = [(excerpts[int(i[1:])-1][0], shortened.get(i, excerpts[int(i[1:])-1][1])) for i in ids]
    for qualifier in ([] if local else _literal_qualifiers(question, qualifiers)):
        if _normalized(qualifier) not in _normalized(" ".join(q for _, q in selected)):
            raise ValueError("requested object qualifier has no source evidence: " + qualifier)
    return selected, {"prompt_count": count, "records": records, "decision": data}


def select_excerpts(model, question, excerpts, settings, today, tags=None, operation="lookup", qualifiers=(), content_budget=None):
    """Compact overlong evidence in bounded batches, then select globally.

    Batch readers retain any requested operand/context. Only the final reader
    judges the complete question. Every retained substring is source checked;
    no unverified summaries or partial totals enter synthesis.
    """
    from .budget import budget_counter
    counter = budget_counter(settings)
    limit = content_budget if content_budget is not None else settings.context_budget_tokens
    if limit <= 0:
        raise ValueError("evidence selection has no content allowance")

    def count(entries, local=False):
        records = [{"id":"C%d" % (n+1), "source":header, "quote":quote}
                   for n,(header,quote) in enumerate(entries)]
        return counter.messages([{"role":"system", "content":LOCAL_SELECTION_RULES if local else SELECTION_RULES},
            {"role":"user", "content":_selection_content(question, records, settings, today, operation,
                                                        qualifiers, limit, local)}])
    if count(excerpts) <= settings.answer_input_budget_tokens:
        return _select_once(model, question, excerpts, settings, today, tags, operation, qualifiers, limit)

    batches, current = [], []
    for header, quote in excerpts:
        # Split literal substrings only when one record cannot fit alone. This
        # preserves the source/date header and never fabricates shorter prose.
        queue = [(header, quote)]
        while queue:
            entry = queue.pop(0)
            if count([entry], True) > settings.answer_input_budget_tokens:
                size = counter.count(entry[1])
                pieces = counter.split(entry[1], max(size // 2, 1))
                if len(pieces) < 2:
                    raise ValueError("one evidence unit and compaction instructions exceed input budget")
                queue = [(header, part) for part in pieces] + queue
                continue
            if current and count(current + [entry], True) > settings.answer_input_budget_tokens:
                batches.append(current)
                current = []
            current.append(entry)
            if len(batches) >= settings.aggregate_max_pages:
                raise ValueError("evidence compaction exceeds aggregate_max_pages")
    if current:
        batches.append(current)
    if not batches or len(batches) > settings.aggregate_max_pages:
        raise ValueError("evidence compaction batches are empty or exceed aggregate_max_pages")
    allowance = limit // len(batches)
    if allowance <= 0:
        raise ValueError("evidence compaction has no per-batch content allowance")
    compacted, audits = [], []
    for number, batch in enumerate(batches, 1):
        selected, audit = _select_once(model, question, batch, settings, today,
            {**dict(tags or {}), "compaction_batch":str(number)}, operation, qualifiers, allowance, local=True)
        # Include separators in the sum, so the combined memory cannot exceed
        # the global content allowance even when all batches retain evidence.
        cost = counter.count("\n\n".join(header + "\n" + quote for header, quote in selected) + ("\n\n" if selected else ""))
        if cost > allowance:
            raise ValueError("compacted batch exceeds its allocated content budget")
        compacted.extend(selected)
        audits.append({**audit, "content_count":cost, "content_allowance":allowance})
    if not compacted or count(compacted) > settings.answer_input_budget_tokens:
        raise ValueError("combined compacted evidence is empty or exceeds input budget")
    selected, audit = _select_once(model, question, compacted, settings, today, tags, operation, qualifiers, limit)
    return selected, {**audit, "mode":"bounded_batch_compaction", "original_records":len(excerpts),
                      "batches":audits, "compacted_records":len(compacted)}


def retry_completion_tokens(model, max_tokens):
    """Double the combined completion allowance, including fixed reasoning room.

    The client adds its headroom to every request. Doubling only the visible
    response allowance would barely increase a reasoning model's actual limit.
    """
    headroom = getattr(getattr(model, "config", None), "output_token_headroom", 0)
    if not isinstance(headroom, int) or isinstance(headroom, bool) or headroom < 0:
        headroom = 0
    return max_tokens * 2 + headroom


def review_response(model, question, answer, evidence, operation, qualifiers, counter, input_limit, tags=None, today=None, verified_calculation=None):
    content = "Caller date (TODAY): %s\nQuestion: %s\n%s\nSupplied memory:\n%s\nProposed answer:\n%s" % (
        today or 'not supplied', question, operation_rules(operation, qualifiers,question),
        json.dumps(evidence, ensure_ascii=False), answer)
    if verified_calculation:
        content += "\nProgram-verified arithmetic (event selection and completeness still need review):\n" + json.dumps(verified_calculation,ensure_ascii=False)
        if verified_calculation.get('unit') == 'weeks' and verified_calculation.get('rounding') == 'complete':
            content += ("\nComplete weeks counts whole seven-day units, allowing remainder days. "
                        "It does not claim an exact multiple of seven days. Review the requested events and "
                        "answer precision; retain weeks when that is the requested unit.")
        if verified_calculation.get('operation') == 'count_dates':
            content += ("\nThese are distinct occupied calendar dates, not elapsed 24-hour periods. "
                        "For recorded_at_past, verify the original user reports recently completed attendance; "
                        "its date is the most recent nonfuture occurrence before that source's recording day. "
                        "Plans, remote undated events and explicit other years cannot use that assumption. "
                        "Verify actual attendance by the requested person, event/date relationship, year and requested "
                        "scope; planned events and assistant examples do not establish attendance.")
    if operation == 'advice':
        content += ("\nThe question is the user's current message. Explicit current self-reports in it are evidence "
                    "even if absent from past memory; questions, hypothetical conditions and historical premises "
                    "do not establish facts. New conditional suggestions are not claims that an item is owned.")
        content += ("\nAssess possession per object. An original report of using/organizing my new/current item "
                    "supports owning it unless explicitly prospective. A question about a case or replacement "
                    "does not negate another item's established ownership. When relevant, advice should build "
                    "on using the established item, rather than suggest buying it as if absent. Repair only "
                    "the unsupported object claim and retain useful advice based on other supported objects.")
    if re.search(r'\bhow long\b', question, re.I):
        content += ("\nA reported month/year duration without an exact start date retains its granularity. "
                    "Its recording date does not justify adding exact days to that approximate duration.")
    rules=RESPONSE_RULES+(DATE_RESPONSE_RULES if operation=='date_difference' else '')
    relation_rules=relative_quantity_rules(evidence,operation)
    if relation_rules:
        rules += '\n' + relation_rules
    messages = [{"role": "system", "content": rules}, {"role": "user", "content": content}]
    if counter and input_limit is not None and counter.messages(messages) > input_limit:
        raise ValueError("answer semantic review exceeds full prompt budget")
    from .user_context import temporal_user_index
    user_index = (literal_user_index(evidence) if operation == 'advice' else
                  literal_user_index(evidence,'quantity') if operation in ('sum_events','count_entities') else
                  temporal_user_index(evidence,question))
    if user_index:
        indexed = [messages[0], {"role":"user", "content":user_index + '\n\n' + content}]
        if not counter or input_limit is None or counter.messages(indexed) <= input_limit:
            messages = indexed
    try:
        data = chat_json(model, messages, purpose="answer_review", max_tokens=600, tags=tags)
    except IncompleteJSONError:
        # An unfinished verdict is discarded. Retry once with a larger bounded
        # completion allowance; authentication and filtering failures propagate.
        data = chat_json(model, messages, purpose="answer_review", max_tokens=retry_completion_tokens(model, 600),
                         tags={**dict(tags or {}), "review_completion_retry": "1"})
    if not isinstance(data, dict) or any(not isinstance(data.get(k), bool) for k in ("responsive", "supported")):
        raise ValueError("answer review needs explicit responsiveness and support decisions")
    return {**data, "prompt_count": counter.messages(messages) if counter else None,
            "scope": "model assessment of responsiveness and personal-fact support; not an independent correctness score"}
