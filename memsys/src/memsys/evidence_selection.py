"""Select complementary source excerpts without claiming semantic completeness.

Facts remain atomic and follow the existing version-chain policy. Their eligible
originals compete alongside retrieved episodes, with source support and missing
query terms preferred over repeated descriptions. No model calls or new writes.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Set

from .schema import Fact, FORGOTTEN, SEARCHABLE_WITH_ARCHIVE
from .textutil import terms


def refresh_trace(selection):
    """Recompute diagnostics after either memory or full-prompt budget pruning."""
    trace = selection.selection_trace
    if not trace:
        return
    episodes = {e.key for e, _ in selection.episodes}
    sources = trace.get('fact_sources', {})
    trace['selected_sources'] = sorted(episodes)
    trace['unsupported_facts'] = [f.key for f in selection.facts
                                if not episodes.intersection(sources.get(f.key, []))]
    actual_matches = {}
    requirements = [set(words) for words in trace.get('query_terms', [])]
    for episode, text in selection.episodes:
        supplied = set(terms(text))
        actual_matches[episode.key] = [index for index, words in enumerate(requirements)
            if words and len(words & supplied) >= max(1, (len(words)+1)//2)]
    trace['supplied_query_matches'] = actual_matches
    trace['covered_queries'] = sorted({index for found in actual_matches.values() for index in found})
    trace['uncovered_queries'] = sorted(set(range(trace.get('query_count', 0)))
                                        - set(trace['covered_queries']))
    # Lexical matches and source IDs are observable, not an entailment proof.
    trace['semantic_completeness_proven'] = False


def select_evidence(retriever, result, plan, budget, today):
    from .read import Candidate, Selection, episode_excerpt, render_memory

    store, settings, counter = retriever.store, retriever.settings, retriever.tokens
    selection = Selection([], [], 0, coverage=dict(result.coverage))
    fact_budget = int(budget * (settings.fact_budget_share_all if plan.needs_all
                               else settings.fact_budget_share))
    max_facts = settings.max_facts_all if plan.needs_all else settings.max_facts
    chosen: Set[int] = set()
    chain_errors: List[str] = []

    def latest(fact):
        visited = {fact.id}
        while fact.superseded_by and fact.superseded_by not in visited:
            visited.add(fact.superseded_by)
            found = store.facts([fact.superseded_by])
            if not found:
                chain_errors.append(fact.key)
                break
            fact = found[0]
        return fact

    # Re-read authoritative state; a stale search result cannot reopen forgetting.
    eligible_facts = {f.id: f for f in store.facts(
        [c.id for c in result.ranked if c.kind == 'fact'], SEARCHABLE_WITH_ARCHIVE['fact'])}
    for candidate in result.ranked:
        if candidate.kind != 'fact' or candidate.id not in eligible_facts:
            continue
        fact = eligible_facts[candidate.id]
        current = latest(fact)
        if plan.focus == 'current' and (current.status == FORGOTTEN or current.superseded_by):
            continue
        bundle = [current] if plan.focus == 'current' else [fact, current]
        bundle = list({f.id: f for f in bundle if f.status != FORGOTTEN and f.id not in chosen}.values())
        alternatives = [bundle]
        if len(bundle) > 1:
            preferred = fact if plan.focus == 'past' else current
            alternatives.append([preferred] if preferred.id not in chosen else [])
        for option in alternatives:
            trial = Selection(selection.facts + option, [], 0)
            if option and len(trial.facts) <= max_facts and counter.count(
                    render_memory(trial, today, settings.tentative_threshold)) <= fact_budget:
                selection.facts.extend(option)
                chosen.update(f.id for f in option)
                break
    selection.omitted_facts = [c.key for c in result.ranked if c.kind == 'fact' and c.id not in chosen]

    sources = store.sources(chosen)
    episode_candidates = {c.id: c for c in result.ranked if c.kind == 'episode'}
    rank_of = {c.id: index for index, c in enumerate(result.ranked) if c.kind == 'episode'}
    source_facts: Dict[int, List[Fact]] = {}
    for fact in selection.facts:
        for eid in sources.get(fact.id, []):
            source_facts.setdefault(eid, []).append(fact)
            if eid not in episode_candidates:
                episode_candidates[eid] = Candidate('episode', eid, via=['selected_fact_source'])
                rank_of[eid] = len(result.ranked)
    episodes = {e.id: e for e in store.episodes(episode_candidates, SEARCHABLE_WITH_ARCHIVE['episode'])}
    # Always use safe views, including originals introduced by version-chain support.
    for eid, episode in episodes.items():
        result.items[episode.key] = episode
    ranked_keys = {c.key for c in result.ranked}
    for fact in selection.facts:
        result.items[fact.key] = fact
        if fact.key not in ranked_keys:
            result.ranked.append(Candidate('fact',fact.id,via=['selected_version']))
            ranked_keys.add(fact.key)
    for eid in episodes:
        candidate = episode_candidates[eid]
        if candidate.key not in ranked_keys:
            result.ranked.append(candidate)
            ranked_keys.add(candidate.key)
    if result.coverage:
        result.coverage['candidates'] = len(result.ranked)
        if len(result.ranked)>settings.aggregate_candidate_limit:
            result.coverage['search_complete'] = False
        selection.coverage = dict(result.coverage)

    query_terms = [set(terms(q)) for q in plan.queries]
    original_terms = set(terms(' '.join(plan.keywords + plan.queries)))
    word_sets = {eid: set(terms(e.text())) for eid, e in episodes.items()}
    frequency = {word: sum(word in words for words in word_sets.values()) for word in original_terms}
    # Require a majority of the content terms; diagnostic only, not a truth label.
    query_matches = {eid: {index for index, words in enumerate(query_terms)
                          if words and len(words & word_sets[eid]) >= max(1, (len(words)+1)//2)}
                     for eid in episodes}
    selected_queries: Set[int] = set()
    selected_words: Set[str] = set()
    supported_facts: Set[int] = set()
    selected_sessions: Set[str] = set()
    decisions = []
    pending = dict(episodes)
    target_slots = min(settings.max_episodes, len(pending), max(
        len(plan.queries), len(selection.facts),
        2 if plan.operation != 'lookup' or plan.needs_all else 1))

    def utility(eid):
        episode = episodes[eid]
        new_queries = query_matches[eid] - selected_queries
        novel = (word_sets[eid] & original_terms) - selected_words
        lexical = sum(1.0/frequency[word] for word in novel)
        new_sources = {f.id for f in source_facts.get(eid, [])} - supported_facts
        rank = rank_of.get(eid, len(result.ranked))
        relevance = 1.0/(rank + 1)
        # For chosen facts, protect a source before consuming redundant context.
        source_priority = bool(new_sources)
        value = relevance + 1.5*len(new_queries) + lexical + 1.25*len(new_sources)
        if selected_sessions and episode.session_id not in selected_sessions:
            value += 0.15
        return (source_priority, value, episode.at if plan.focus == 'current' else -rank,
                -eid)

    while pending and len(selection.episodes) < settings.max_episodes:
        eid = max(pending, key=utility)
        episode = pending.pop(eid)
        selection.episodes.append((episode,''))
        overhead = counter.count(render_memory(selection,today,settings.tentative_threshold))
        selection.episodes.pop()
        remaining = max(budget-overhead-1, 0)
        slots = max(1, target_slots-len(selection.episodes))
        cap = min(settings.episode_max_tokens, remaining//slots)
        focus_terms = [f.value or f.text for f in source_facts.get(eid, [])]
        focused_plan = replace(plan, queries=plan.queries+focus_terms)
        candidate = episode_candidates[eid]
        text = (episode_excerpt(episode,focused_plan,cap,candidate.chunks,counter=counter)
                if settings.query_focused_excerpts else counter.truncate(episode.text(),cap))
        if not text:
            continue
        selection.episodes.append((episode,text))
        if counter.count(render_memory(selection,today,settings.tentative_threshold)) > budget:
            selection.episodes.pop()
            continue
        # Coverage belongs to the supplied excerpt, not its unabridged source.
        # Counting clipped-away terms would suppress complementary evidence.
        supplied_words = set(terms(text))
        supplied_queries = {index for index, words in enumerate(query_terms)
            if words and len(words & supplied_words) >= max(1, (len(words)+1)//2)}
        selected_queries.update(supplied_queries)
        selected_words.update(supplied_words & original_terms)
        supported_facts.update(f.id for f in source_facts.get(eid, []))
        selected_sessions.add(episode.session_id)
        decisions.append(dict(source=episode.key, cap=cap, original_rank=rank_of[eid],
            supporting_facts=[f.key for f in source_facts.get(eid, [])],
            query_matches=sorted(supplied_queries),
            retrieved_query_matches=sorted(query_matches[eid]), truncated=text!=episode.text()))

    selected_ids = {e.id for e,_ in selection.episodes}
    selection.omitted_episodes = ['E%d'%eid for eid in episode_candidates if eid not in selected_ids]
    selection.context_tokens = counter.count(render_memory(selection,today,settings.tentative_threshold))
    selection.selection_trace = dict(selection_policy='coverage', query_count=len(plan.queries),
        query_terms=[sorted(words) for words in query_terms],
        match_scope='lexical content-term coverage of retrieved safe originals; not semantic completeness',
        query_matches={'E%d'%eid: sorted(found) for eid,found in query_matches.items()},
        fact_sources={f.key: ['E%d'%eid for eid in sources.get(f.id, [])] for f in selection.facts},
        decisions=decisions, broken_current_chains=chain_errors)
    refresh_trace(selection)
    if plan.needs_all:
        selection.coverage['context_complete'] = bool(result.coverage.get('search_complete')) and bool(
            result.ranked) and not selection.omitted_facts and not selection.omitted_episodes and all(
                text==episode.text() for episode,text in selection.episodes)
    return selection
