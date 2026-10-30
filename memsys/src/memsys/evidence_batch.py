"""Bounded page review for lists and comparisons, followed by source-backed synthesis."""
from __future__ import annotations

import json
import re

from . import prompts
from .aggregate_batch import aggregation_pages, derived_coverage, materialize_item, source_speaker
from .aggregation import answer_with_validation, is_temporal_interval, wants_calculation
from .budget import budget_counter
from .llm import BadJSONError, IncompleteJSONError, ModelError, chat_json
from .read_budget import is_read_service_error
from .queryops import collection_rules, is_attendance_day_count, operation_for, operation_rules
from .semantic_review import retry_completion_tokens, select_excerpts
from .schema import Episode, Fact
from .user_context import literal_user_index, temporal_user_index

RULES = ("Review this page of memories for the question. Return JSON only with keys reviewed and items. "
         "reviewed must list every supplied source id, including irrelevant sources. Each item has source "
         "and quote: copy an exact contiguous excerpt that is relevant to the question. Preserve names, "
         "list order, recommendation scope, dates and the distinction between user statements and assistant "
         "suggestions. Copy enough context to establish the requested relationship. Do not answer the question, "
         "summarize, invent quotations or calculate. An empty items list is allowed after reviewing all sources. "
         "Do not select the final latest state on this page. Also retain earlier passages that identify the object "
         "or conditions referred to by later updates, pronouns or cumulative values; an obsolete number can still "
         "supply necessary identity context. The global selection step will resolve states across pages. "
         "Entries marked context_only have their originals elsewhere: use those originals as evidence.")


def _validate(data, page):
    if not isinstance(data, dict) or not isinstance(data.get("reviewed"), list) or set(data["reviewed"]) != set(page):
        raise ValueError("page review did not cover every supplied source")
    items = data.get("items")
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("page evidence must contain at most 100 excerpts")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("page evidence must contain objects")
        source, quote = item.get("source"), item.get("quote")
        if not isinstance(source, str) or source not in page or not page[source]["can_contribute"]:
            raise ValueError("evidence source is missing or context_only")
        if not isinstance(quote, str) or not quote.strip() or quote not in page[source]["text"]:
            raise ValueError("evidence quote is not an exact contiguous source excerpt")
    return items


def identity_context(excerpts, result, settings, counter):
    """Retain literal user context near selected original episodes.

    Flat evidence pages can separate an amount from the preceding turn that
    names its object. Source/session/round links establish adjacency without
    asking a model to invent an alias or trust a question's premise.
    """
    expanded = list(excerpts)
    seen = {(re.match(r"\[([^]]+)\]", h).group(1), q) for h, q in expanded}
    added = []
    episodes = [e for e in result.items.values() if isinstance(e, Episode)]
    for header, quote in excerpts:
        source = re.match(r"\[([^]]+)\]", header).group(1)
        item = result.items.get(source)
        if not isinstance(item, Episode):
            continue
        neighbors = [e for e in episodes if e.session_id == item.session_id and
                     e.round_idx in (0, item.round_idx - 1, item.round_idx)]
        for e in sorted(neighbors, key=lambda e: (e.round_idx, e.id)):
            if not e.user_text:
                continue
            text = counter.truncate("User: " + e.user_text, settings.episode_max_tokens)
            if not text or any(key == e.key and text in existing for key, existing in seen):
                continue
            # A full user statement already contains a shorter selected quote;
            # replace that substring rather than paying for both copies.
            contained = {(key, q) for key, q in seen if key == e.key and q in text}
            if contained:
                expanded = [(h, q) for h, q in expanded
                            if (re.match(r"\[([^]]+)\]", h).group(1), q) not in contained]
                seen -= contained
            prefix = "[%s] %s session=%s round=%s speaker=user" % (e.key, e.at.date(), e.session_id, e.round_idx)
            expanded.append((prefix, text))
            seen.add((e.key, text))
            added.append({"source": e.key, "anchor": source, "round": e.round_idx,
                          "truncated": text != "User: " + e.user_text})
    return expanded, added


def prepare_complete_context(question, result, origins, settings, today, operation=None, qualifiers=()):
    """Pure preparation: validate every original and the exact synthesis budget."""
    if not settings.complete_episode_context or not settings.page_semantics or not result.coverage.get('search_complete'):
        return None
    episodes = [result.items[c.key] for c in result.ranked if isinstance(result.items.get(c.key), Episode)]
    original_ids = {e.id for e in episodes}
    if not episodes or len(original_ids) != len(episodes) or len(episodes) > settings.aggregate_contribution_limit:
        return None
    for candidate in result.ranked:
        item = result.items.get(candidate.key)
        if isinstance(item, Fact):
            linked = set(origins.get(candidate.key, ()))
            if not linked or not linked <= original_ids:
                return None
        elif not isinstance(item, Episode):
            return None
    operation = operation or operation_for(question)
    temporal, numeric = is_temporal_interval(question), (wants_calculation(question, operation) or is_attendance_day_count(question))
    counter = budget_counter(settings)
    evidence, blocks = {}, []
    for e in sorted(episodes, key=lambda e: (e.at.isoformat(), e.session_id, e.round_idx, e.id)):
        text = e.text()
        if not text.strip():
            return None
        header = '[%s] %s session=%s round=%s speaker=%s' % (
            e.key, e.at.date(), e.session_id, e.round_idx, source_speaker(e, text))
        block = header + '\n' + text
        evidence[e.key] = block
        blocks.append(block)
    memory = '\n\n'.join(blocks)
    rules = (prompts.DATE_ANSWER_RULES if temporal else prompts.ANSWER_RULES) + '\n' + operation_rules(operation, qualifiers,question)
    rules += '\n' + prompts.SOURCE_TIME_RULES
    if not temporal and not numeric and operation in ('lookup','latest_state','historical_state','compare_states','list','ratio'):
        rules += '\n' + prompts.QUALIFIED_RECALL_RULES
    if temporal or numeric:
        rules += '\n\n' + (prompts.DATE_CALCULATION_RULES if temporal else prompts.calculation_rules(question))
    messages = [{'role':'system', 'content':prompts.ANSWER_SYSTEM}, {'role':'user', 'content':
        prompts.ANSWER_QA.format(today=today, memory=memory, question=question, rules=rules)}]
    memory_count, prompt_count = counter.count(memory), counter.messages(messages)
    if memory_count > settings.context_budget_tokens or prompt_count > settings.answer_input_budget_tokens:
        return None
    user_index = (literal_user_index(evidence) if operation == 'advice' else
                  literal_user_index(evidence,'quantity') if operation in ('sum_events','count_entities') else
                  temporal_user_index(evidence,question))
    index_applied = False
    if user_index:
        indexed_memory = user_index + '\n\nFull original conversations:\n\n' + memory
        indexed_messages = [messages[0], {'role':'user', 'content':
            prompts.ANSWER_QA.format(today=today, memory=indexed_memory, question=question, rules=rules)}]
        if (counter.count(indexed_memory) <= settings.context_budget_tokens and
                counter.messages(indexed_messages) <= settings.answer_input_budget_tokens):
            messages = indexed_messages
            memory_count, prompt_count = counter.count(indexed_memory), counter.messages(messages)
            index_applied = True
    return dict(messages=messages, evidence=evidence, memory=memory,
                memory_count=memory_count, prompt_count=prompt_count,
                index_applied=index_applied, user_index_count=counter.count(user_index) if index_applied else 0,
                temporal=temporal, numeric=numeric, operation=operation,
                binding=_complete_binding(question, origins, settings, today, operation, qualifiers))


def _complete_binding(question, origins, settings, today, operation, qualifiers):
    return (question, str(today), operation or operation_for(question), tuple(qualifiers),
            settings.read_fingerprint(), tuple(sorted((key, tuple(sorted(value))) for key,value in origins.items())))


def complete_answer(model, question, result, origins, settings, today, tags=None, operation=None, qualifiers=()):
    """Synthesize every eligible original while retaining proof and support review."""
    prepared = getattr(result, 'complete_context', None)
    if prepared is None or prepared['binding'] != _complete_binding(question, origins, settings, today, operation, qualifiers):
        prepared = prepare_complete_context(question, result, origins, settings, today, operation, qualifiers)
    if prepared is None:
        return None
    messages, evidence, memory = prepared['messages'], prepared['evidence'], prepared['memory']
    memory_count, prompt_count = prepared['memory_count'], prepared['prompt_count']
    temporal, numeric, operation = prepared['temporal'], prepared['numeric'], prepared['operation']
    counter = budget_counter(settings)
    notes, answer, generation = answer_with_validation(model, messages, evidence=evidence,
        origins=origins, enabled=(temporal or numeric) and settings.aggregation_validation,
        question=question, max_tokens=settings.answer_max_tokens, tags=tags,
        counter=counter, input_limit=settings.answer_input_budget_tokens, today=str(today),
        semantic_enabled=settings.operation_semantics, operation=operation, qualifiers=qualifiers,
        review_policy=settings.review_policy, review_context=('complete_original_context',),
        require_calculation=numeric)
    audit = dict(status='evidence_checked', mode='complete_original_context',
        coverage=dict(result.coverage), pages_complete=True, pages_processed=0,
        reviewed_sources=sorted(evidence), derived_sources_covered_by_originals=derived_coverage(result, origins),
        synthesis_memory_count=memory_count, synthesis_prompt_count=prompt_count,
        original_context=memory,
        user_statement_index={'applied':prepared['index_applied'], 'count':prepared['user_index_count']},
        generation_attempts=generation['generation_attempts'], answer_validation=generation,
        scope='Program copies every eligible original in full within both input bounds; synthesis relevance, state and event qualification are model judgments. Arithmetic/date proof and support review remain enabled.')
    if generation['status'] in ('invalid', 'incomplete'):
        audit.update(status=generation['status'], error='answer evidence or semantics did not validate')
    if temporal:
        audit['date_validation'] = generation
    return notes, answer, audit


def paged_answer(model, question, result, origins, settings, today, tags=None, operation=None, qualifiers=(), require_calculation=False):
    operation = operation or operation_for(question)
    counter = budget_counter(settings)
    audit = {"status": "incomplete", "mode": "paged_evidence",
             "coverage": dict(result.coverage), "pages_processed": 0,
             "scope": "candidate coverage and literal excerpts are checked; relevance and synthesis remain model judgments",
             "page_validation_failures": [], "page_attempt_prompt_counts": [], "page_outputs": []}
    refusal = ("无法确认已覆盖并核验所有相关记忆，暂时不能给出完整回答。" if re.search(r"[\u4e00-\u9fff]", question)
               else "I don't have enough verified information, so I cannot give a complete answer.")
    try:
        if not result.coverage.get("search_complete"):
            raise ValueError("eligible pool exceeded aggregate candidate limit")
        pages = aggregation_pages(result, settings, today, origins)
        if settings.page_semantics:
            audit['derived_sources_covered_by_originals'] = derived_coverage(result, origins)
        audit["pages_total"] = len(pages)
        if not pages or len(pages) > settings.aggregate_max_pages:
            raise ValueError("evidence pages are empty or exceed aggregate_max_pages")
        excerpts, seen = [], set()
        for number, page in enumerate(pages, 1):
            rules = RULES
            if settings.page_semantics:
                rules += (" For these numbered sources, cite every item using source, line_start and line_end "
                          "(inclusive integer L-number range). Omit quote: the program copies the original lines. "
                          'Example item: {"source":"E1","line_start":1,"line_end":2}. '
                          "A full line range can preserve relationship context without copying or rewriting text. "
                          + collection_rules(operation, qualifiers))
            messages = [{"role": "system", "content": rules}, {"role": "user", "content":
                "Today: %s\nQuestion: %s\n\n%s" % (today, question, "\n\n".join(e["rendered"] for e in page.values()))}]
            completion_tokens = settings.extract_max_tokens
            for attempt in range(2):
                count = counter.messages(messages)
                if count > settings.answer_input_budget_tokens:
                    raise ValueError("evidence page exceeds full prompt budget")
                audit["page_attempt_prompt_counts"].append(count)
                try:
                    data = chat_json(model, messages, purpose="evidence_page" if attempt == 0 else "evidence_page_repair",
                                     max_tokens=completion_tokens, tags=tags)
                except ModelError as exc:
                    if exc.account_problem or is_read_service_error(exc):
                        raise
                    audit["page_validation_failures"].append({"page": number, "attempt": attempt + 1, "error": str(exc)})
                    if attempt:
                        raise
                    if isinstance(exc, IncompleteJSONError):
                        completion_tokens = retry_completion_tokens(model, settings.extract_max_tokens)
                    # An unfinished draft is not valid evidence to replay.
                    messages += [{"role": "user", "content":
                        "The page response was unavailable or incomplete. Return compact complete JSON, "
                        "reviewing every source. Prefer line references over copied passages; retain all relevant evidence."}]
                    continue
                try:
                    audit["page_outputs"].append({"page": number, "attempt": attempt + 1, "data": data})
                    if isinstance(data, dict) and isinstance(data.get("items"), list):
                        data = {**data, "items": [materialize_item(item, page) for item in data["items"]]}
                    items = _validate(data, page)
                    break
                except (ValueError, TypeError, KeyError) as exc:
                    audit["page_validation_failures"].append({"page": number, "attempt": attempt + 1, "error": str(exc)})
                    if attempt:
                        raise
                    messages += [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                                 {"role": "user", "content": "Evidence validation failed: %s. Re-read every supplied source and return corrected JSON. For numbered sources use source, line_start and line_end, omitting quote; the program will copy the original text." % exc}]
            for item in items:
                key = (item["source"], item["quote"])
                if key not in seen:
                    header = page[item["source"]]["rendered"].split("\n", 1)[0]
                    speaker = source_speaker(result.items.get(item["source"]), item["quote"])
                    excerpts.append((header + " speaker=" + speaker, item["quote"]))
                    seen.add(key)
                    if len(excerpts) > settings.aggregate_contribution_limit:
                        raise ValueError("evidence exceeds aggregate_contribution_limit")
            audit["pages_processed"] += 1
        if not excerpts:
            raise ValueError("no relevant verified evidence was selected")
        temporal = is_temporal_interval(question)
        numeric = wants_calculation(question, operation) or require_calculation
        def answer_messages(memory):
            rules = (prompts.DATE_ANSWER_RULES if temporal else prompts.ANSWER_RULES) + "\n" + operation_rules(operation, qualifiers,question)
            if temporal or numeric:
                rules += "\n\n" + (prompts.DATE_CALCULATION_RULES if temporal else prompts.calculation_rules(question))
            return [{"role": "system", "content": prompts.ANSWER_SYSTEM}, {"role": "user", "content":
                prompts.ANSWER_QA.format(today=today, memory=memory, question=question, rules=rules)}]
        scaffold = counter.messages(answer_messages(""))
        content_limit = min(settings.context_budget_tokens, max(settings.answer_input_budget_tokens - scaffold, 0))
        audit.update(synthesis_content_limit=content_limit, synthesis_scaffold_count=scaffold)
        if not content_limit:
            raise ValueError("question and required synthesis instructions exceed input budget")
        if settings.page_semantics:
            excerpts, added = identity_context(excerpts, result, settings, counter)
            audit["identity_context"] = added
            if len(excerpts) > settings.aggregate_contribution_limit:
                raise ValueError("identity context exceeds aggregate_contribution_limit")
            audit["candidate_excerpts"] = excerpts
            full_memory = "\n\n".join(header + "\n" + quote for header, quote in excerpts)
            if counter.count(full_memory) <= content_limit:
                # Preserve complete verified context whenever it already fits.
                # Selection is needed for compaction, not a second sufficiency
                # gate that can lose identity links or relative-date relations.
                selection = {'mode': 'all_verified_evidence',
                             'sufficiency': 'checked during answering, support review and required date proof'}
            else:
                excerpts, selection = select_excerpts(model, question, excerpts, settings, today, tags, operation, qualifiers,
                                                       content_budget=content_limit)
            audit["semantic_selection"] = selection
        memory = "\n\n".join(header + "\n" + quote for header, quote in excerpts)
        if counter.count(memory) > content_limit:
            raise ValueError("complete evidence exceeds synthesis context budget")
        messages = answer_messages(memory)
        evidence = {}
        for header, quote in excerpts:
            source = re.match(r"\[([^]]+)\]", header).group(1)
            evidence[source] = evidence.get(source, "") + header + "\n" + quote + "\n"
        notes, answer, generation = answer_with_validation(model, messages, evidence=evidence,
            origins=origins, enabled=(temporal or numeric) and settings.aggregation_validation,
            question=question, max_tokens=settings.answer_max_tokens, tags=tags,
            counter=counter, input_limit=settings.answer_input_budget_tokens, today=str(today),
            semantic_enabled=settings.operation_semantics, operation=operation, qualifiers=qualifiers,
            review_policy=settings.review_policy, review_context=("cross_page_synthesis",),
            require_calculation=require_calculation)
        audit.update(status="evidence_checked", pages_complete=True, excerpts=len(excerpts),
                     reviewed_sources=sorted({key for page in pages for key in page}),
                     synthesis_memory_count=counter.count(memory), synthesis_prompt_count=counter.messages(messages),
                     generation_attempts=generation["generation_attempts"])
        audit["answer_validation"] = generation
        if generation["status"] in ("invalid", "incomplete"):
            audit.update(status=generation["status"], error="answer evidence or semantics did not validate")
        if temporal:
            audit["date_validation"] = generation
        return notes, answer, audit
    except (ValueError, TypeError, KeyError, BadJSONError, ModelError) as exc:
        if isinstance(exc, ModelError) and (exc.account_problem or is_read_service_error(exc)):
            raise
        audit.update(error=str(exc), pages_complete=False)
        return "Candidate coverage or evidence could not be verified.", refusal, audit
