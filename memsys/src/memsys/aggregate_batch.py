"""聚合候选分批阅读：逐页核验来源与覆盖，然后用程序合并贡献。"""
from __future__ import annotations

import json
import re
from decimal import Decimal, localcontext

from .aggregation import _clean, _decimal, _quantity_decimal, _unit, inspect_calculation, quantity_repair_rules
from .budget import budget_counter
from .llm import BadJSONError, IncompleteJSONError, ModelError, chat_json
from .read_budget import is_read_service_error
from .schema import Episode, Fact
from .queryops import collection_rules, operation_for
from .semantic_review import reconcile, retry_completion_tokens

RULES = ("Read this page of candidate memories for the question. Return one JSON object with keys "
         '"reviewed" (every source id on this page), "operation", "unit" and "items". '
         'Choose exactly one operation: "sum", "count_unique", "count_groups" or "unsupported". '
         'For sum each item has "source", "quote" (exact contiguous excerpt), "value" (numeric literal) '
         'and "unit". Sum requires digits or literal English quantity words adjacent to their measurement '
         'unit, including adjectival durations such as 4-day; currency amounts require digits. '
         'An ordinal immediately before a literal count unit additionally needs ordinal_progress=true and '
         'value_type=cumulative, proving completed progress rather than rank or future plans; never use this for measurements. '
         'For an exact time-unit conversion also give source_value and source_unit copied from the source '
         '(e.g. value=7, unit=days, source_value=1, source_unit=weeks). Only seconds/minutes/hours/days/weeks '
         'conversions are supported; never convert currencies or approximate calendar months. '
         'For a quoted date-range duration, give duration={"start_text":"quoted start date",'
         '"end_text":"quoted end date"} and the computed elapsed value in the requested time unit. '
         'The exact range connector must be in the quote. Shortened endpoints inherit their month/year; '
         'an omitted start year uses the source date header. Do not invent dates for vague durations. '
         'For count_unique each item has "source", "quote" and "entity" (literal name from the quote). '
         'Use count_unique for individually identified objects or names, even when no numeral appears. '
         'For count_groups each item has "source", "quote", "value", "unit" and "quantity_label" '
         '(noun phrase copied from the quote, including any intervening adjectives). Do not replace a '
         'pronoun noun phrase with a category noun from the question. An explicit integer in digits or English words zero through '
         'ninety-nine must immediately precede that noun. Use count_groups for group sizes, including '
         'English word quantities and explicit cumulative counts of unnamed objects; retain the earlier '
         'context that identifies pronouns such as ones. Different literal category labels can use count_groups with their own copied '
         'quantity_label. A completed-progress ordinal additionally requires ordinal_progress=true and '
         'value_type=cumulative; a rank, list position, preference or future occurrence is not a completed count. '
         'Do not use sum for these. The top-level unit and each numeric item unit '
         'must agree. Choose a consistent unit for the question across pages. '
         "Include only contributions relevant to the question. Ignore outdated cumulative states when the latest "
         "state is requested. Distinguish independent events from cumulative progress, plans from completed actions, "
         "and payments from event dates. Facts and original episodes can describe the same event: never count both. "
         "An empty items list is allowed after reviewing all page sources. Use unsupported as operation if these "
         "operations cannot answer the question. Entries marked context_only help interpretation but "
         "must not contribute: their original episodes are supplied elsewhere in this candidate set. "
         "Do not compute a partial page total as a complete answer.")

SEMANTIC_PAGE_RULES = ("\nThe records will be reconciled across all pages before arithmetic. Include all relevant "
    "snapshots and repeated mentions with their context, rather than guessing which one is latest on this page. "
    "For each numeric item also give value_type (independent, cumulative, state or unknown) and scope "
    "(the object/activity measured). Do not replace a requested subset quantity with an overall group size. "
    "For these numbered sources, cite every item with source plus line_start and line_end "
    "(inclusive integer L-number range). Omit quote; the program copies those original lines. "
    'Example citation fields: {"source":"E1","line_start":1,"line_end":2}. '
    "Keep enough text to establish the scope and whether amounts are cumulative or incremental.")


def source_speaker(original, quote):
    """Attribute a literal excerpt from its stored turn, never from model metadata."""
    if isinstance(original, Fact):
        return "derived"
    if not isinstance(original, Episode) or not isinstance(quote, str) or not quote.strip():
        return "ambiguous"
    user = bool(original.user_text and quote in "User: " + original.user_text)
    assistant = bool(original.assistant_text and quote in "Assistant: " + original.assistant_text)
    if user and assistant:
        return "ambiguous"
    if user:
        return "user"
    if assistant:
        return "assistant"
    return "mixed" if quote in original.text() else "ambiguous"


def materialize_item(item, page):
    """Copy source text from verified line references; model quotations remain supported."""
    if not isinstance(item, dict):
        raise ValueError("page evidence must be objects")
    source = item.get("source")
    if "line_start" not in item and "line_end" not in item:
        return dict(item)
    if source not in page:
        raise ValueError("line citation source is unavailable")
    start, end = item.get("line_start"), item.get("line_end")
    lines = page[source]["text"].splitlines(keepends=True)
    if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool)
            or not 1 <= start <= end <= len(lines)):
        raise ValueError("line citation range is invalid")
    return {**item, "quote": "".join(lines[start - 1:end])}


def _page_parts(text, prefix, counter, limit, numbered):
    allowance = limit - counter.count(prefix + "\n\n")
    if allowance <= 0:
        raise ValueError("source header exceeds page content budget")
    queue = list(counter.split(text, allowance))
    while queue:
        part = queue.pop(0)
        rendered = prefix + "\n" + ("\n".join("L%d: %s" % (n, line.rstrip("\r\n"))
            for n, line in enumerate(part.splitlines(keepends=True), 1)) if numbered else part)
        cost = counter.count(rendered + "\n\n")
        if cost > limit:
            smaller = counter.split(part, max(counter.count(part) // 2, 1))
            if len(smaller) <= 1:
                raise ValueError("a source unit and line labels cannot fit the page content budget")
            queue = list(smaller) + queue
            continue
        yield part, rendered, cost


def aggregation_pages(result, settings, today, origins=None):
    """只分页检索到的候选；每段原文完整覆盖且不超过单次内容预算。"""
    counter = budget_counter(settings)
    limit = settings.context_budget_tokens
    pages, page, used = [], {}, 0
    original_ids = {candidate.id for candidate in result.ranked if candidate.kind == "episode"}
    for candidate in result.ranked:
        item = result.items[candidate.key]
        fact_origins = set((origins or {}).get(candidate.key, set())) if isinstance(item, Fact) else set()
        # A derived summary adds no independent evidence when ALL its originals
        # are already read. Avoid asking the model to cite then ignore that summary.
        if settings.page_semantics and fact_origins and fact_origins <= original_ids:
            continue
        prefix = "[%s] %s" % (candidate.key, item.when().label() if isinstance(item, Fact) else item.at.date())
        if not isinstance(item, Fact):
            prefix += " session=%s round=%s" % (item.session_id, item.round_idx)
        if isinstance(item, Fact):
            prefix += " status=%s" % item.status
            if settings.page_semantics:
                prefix += " noted=" + item.recorded_at.date().isoformat()
                if item.semantics:
                    prefix += " annotations=" + json.dumps(item.semantics, ensure_ascii=False)
            if item.superseded_by:
                prefix += " replaced_by=F%d" % item.superseded_by
        original = sorted(fact_origins) if fact_origins and fact_origins <= original_ids else []
        if original:
            prefix += " context_only; contribute using " + ",".join("E%d" % eid for eid in original)
        text = item.text if isinstance(item, Fact) else item.text()
        for part, rendered, cost in _page_parts(text, prefix, counter, limit, settings.page_semantics):
            if page and (candidate.key in page or used + cost > limit or len(page) >= settings.aggregate_page_items):
                pages.append(page)
                page, used = {}, 0
            page[candidate.key] = {"text": part, "rendered": rendered, "can_contribute": not bool(original)}
            used += cost
    if page:
        pages.append(page)
    return pages


def derived_coverage(result, origins):
    original_ids = {c.id for c in result.ranked if c.kind == "episode"}
    return {c.key: sorted((origins or {}).get(c.key, set())) for c in result.ranked
            if c.kind == "fact" and (origins or {}).get(c.key) and
            set(origins[c.key]) <= original_ids}


def _spec(items, operation, unit):
    with localcontext() as context:
        context.prec = 200
        value = (sum((_quantity_decimal(i.get("value")) for i in items), Decimal(0)) if operation in ("sum", "count_groups")
                 else Decimal(len({_clean(i.get("entity", "")) for i in items})))
    return {"operation": operation, "unit": unit, "items": items, "result": format(value, "f")}


def _validate(spec, evidence, origins, max_items):
    output = "CALCULATION: " + json.dumps(spec, ensure_ascii=False) + "\nANSWER: " + spec["result"]
    return inspect_calculation(output, evidence, origins, max_items=max_items)[1]


def _validate_page(data, page, origins, operation, unit):
    if not isinstance(data, dict) or not isinstance(data.get("reviewed"), list) or set(data["reviewed"]) != set(page):
        raise ValueError("page review did not cover every supplied source")
    op, page_unit, items = data.get("operation"), data.get("unit"), data.get("items")
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise ValueError("page items must be a list of objects")
    if not items:
        return operation, unit, items
    if op not in ("sum", "count_unique", "count_groups") or not isinstance(page_unit, str) or not page_unit.strip():
        raise ValueError("unsupported operation or missing unit")
    if operation and (op != operation or _unit(page_unit) != _unit(unit)):
        raise ValueError("pages disagree on operation or unit")
    if any(i.get("source") in page and not page[i["source"]]["can_contribute"] for i in items):
        raise ValueError("context-only facts cannot contribute when their original episodes are supplied")
    checked = _validate(_spec(items, op, page_unit),
        {k: v["rendered"].split("\n", 1)[0] + "\n" + v["text"] for k, v in page.items()}, origins, 100)
    if checked["status"] != "checks_passed":
        raise ValueError("page evidence failed: " + "; ".join(checked.get("errors", [])))
    return op, page_unit, items


def paged_aggregate(model, question, result, origins, settings, today, tags=None, operation=None, qualifiers=()):
    operation = operation or operation_for(question)
    counter = budget_counter(settings)
    audit = {"status": "incomplete", "coverage": dict(result.coverage), "pages_processed": 0,
             "scope": "all retrieved candidate text is paged; semantic relevance and identity remain model judgments"}
    refusal = ("无法确认已覆盖并核验所有相关记忆，暂时不能给出完整合计。" if re.search(r"[\u4e00-\u9fff]", question)
               else "I don't have enough verified information, so I cannot give a complete total.")
    try:
        if not result.coverage.get("search_complete"):
            raise ValueError("eligible pool exceeded aggregate candidate limit")
        pages = aggregation_pages(result, settings, today, origins)
        if settings.page_semantics:
            audit['derived_sources_covered_by_originals'] = derived_coverage(result, origins)
        audit["pages_total"] = len(pages)
        if not pages or len(pages) > settings.aggregate_max_pages:
            raise ValueError("aggregation pages are empty or exceed aggregate_max_pages")
        combined, seen = [], set()
        calculation_op = unit = None
        page_counts = []
        audit["page_validation_failures"] = []
        audit["page_outputs"] = []
        audit["page_attempt_prompt_counts"] = []
        for page_number, page in enumerate(pages, 1):
            rules = RULES
            if settings.page_semantics:
                rules += SEMANTIC_PAGE_RULES + "\n" + collection_rules(operation, qualifiers)
            if calculation_op:
                rules += "\nEstablished calculation: operation=%s, unit=%s. Keep these for compatible contributions; use only the exact time-unit conversions described above." % (calculation_op, unit)
            messages = [{"role": "system", "content": rules}, {"role": "user", "content":
                "Today: %s\nQuestion: %s\n\n%s" % (today, question, "\n\n".join(e["rendered"] for e in page.values()))}]
            count = counter.messages(messages)
            if count > settings.answer_input_budget_tokens:
                raise ValueError("aggregate page exceeds full prompt budget")
            completion_tokens = settings.extract_max_tokens
            for attempt in range(2):
                prompt_count = counter.messages(messages)
                if prompt_count > settings.answer_input_budget_tokens:
                    raise ValueError("aggregate page repair exceeds full prompt budget")
                audit["page_attempt_prompt_counts"].append(prompt_count)
                try:
                    data = chat_json(model, messages, purpose="aggregate_page" if attempt == 0 else "aggregate_page_repair",
                                     max_tokens=completion_tokens, tags=tags)
                except ModelError as exc:
                    if exc.account_problem or is_read_service_error(exc):
                        raise
                    audit["page_validation_failures"].append({"page": page_number, "attempt": attempt + 1, "error": str(exc)})
                    if attempt:
                        raise
                    if isinstance(exc, IncompleteJSONError):
                        completion_tokens = retry_completion_tokens(model, settings.extract_max_tokens)
                    messages += [{"role": "user", "content":
                        "The page response was unavailable or incomplete. Return compact complete JSON, "
                        "reviewing every source. Prefer line references over copied passages; retain all relevant contributions."}]
                    continue
                try:
                    audit["page_outputs"].append({"page": page_number, "attempt": attempt + 1, "data": data})
                    if isinstance(data, dict) and isinstance(data.get("items"), list):
                        data = {**data, "items": [materialize_item(item, page) for item in data["items"]]}
                    op, page_unit, items = _validate_page(data, page, origins, calculation_op, unit)
                    break
                except (ValueError, TypeError, KeyError) as exc:
                    audit["page_validation_failures"].append({"page": page_number, "attempt": attempt + 1, "error": str(exc)})
                    if attempt:
                        raise
                    messages += [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                                 {"role": "user", "content": "The page evidence did not validate: %s. Re-read every supplied source and return corrected JSON using the stated schemas. For numbered sources use source, line_start and line_end instead of copying quote. Do not invent quantities or return a partial total." % exc + quantity_repair_rules((str(exc),))}]
            if not items:
                audit["pages_processed"] += 1
                page_counts.append(count)
                continue
            calculation_op, unit = op, page_unit
            for item in items:
                key = (item.get("source"), item.get("quote"), item.get("entity"), str(item.get("value")))
                if key not in seen:
                    original = result.items.get(item.get("source"))
                    recorded = original.recorded_at if isinstance(original, Fact) else original.at if original else None
                    combined.append({**item, "recorded_at": recorded.isoformat() if recorded else "",
                                     "speaker": source_speaker(original, item.get("quote"))})
                    seen.add(key)
                    if len(combined) > settings.aggregate_contribution_limit:
                        raise ValueError("verified contributions exceed aggregate_contribution_limit")
            audit["pages_processed"] += 1
            page_counts.append(count)
        if not combined:
            raise ValueError("no verified contributions; an empty extraction cannot establish a zero total")
        audit["candidate_contributions"] = combined
        if settings.page_semantics:
            combined, reconciliation = reconcile(model, question, combined, settings, today, tags, operation, qualifiers)
            audit["semantic_reconciliation"] = reconciliation
        spec = _spec(combined, calculation_op, unit)
        evidence = {key: ("[%s] noted %s\n%s" % (key, item.recorded_at.date(), item.text)
                         if isinstance(item, Fact) else "[%s] %s\n%s" % (key, item.at.date(), item.text()))
                    for key, item in result.items.items()}
        checked = _validate(spec, evidence, origins, settings.aggregate_contribution_limit)
        if checked["status"] != "checks_passed":
            raise ValueError("combined evidence failed: " + "; ".join(checked.get("errors", [])))
        audit.update(checked, pages_complete=True, page_prompt_counts=page_counts,
                     reviewed_sources=sorted({k for page in pages for k in page}), contributions=len(combined))
        value = spec["result"]
        answer = value if re.search(r"only.*(?:number|total)|只.*(?:数字|数值)", question, re.I) else value + " " + unit
        return "All candidate pages were reviewed and their quoted contributions validated.", answer, audit
    except (ValueError, TypeError, KeyError, BadJSONError, ModelError) as exc:
        if isinstance(exc, ModelError) and (exc.account_problem or is_read_service_error(exc)):
            raise
        audit.update(error=str(exc), pages_complete=False)
        return "Candidate coverage or evidence could not be verified.", refusal, audit
