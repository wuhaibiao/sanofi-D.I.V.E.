"""Bounded original-text RAG: no fact extraction, query generation or answer review."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import re

from .budget import budget_counter
from .indexing import ChunkInput, EpisodeIndex
from .read import SearchResult, Selection
from .read_budget import check_read_budget
from .schema import SEARCHABLE_WITH_ARCHIVE
from .textutil import terms
from .vecmath import normalize


def chunk_ranges(text, size, overlap):
    """Overlapping literal spans, preferring paragraph/sentence ends near the cap."""
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            lower = start + size // 2
            breaks = [m.end() for m in re.finditer(r'\n\s*\n|[.!?。！？](?:\s+|$)', text[lower:end])]
            if breaks:
                end = lower + breaks[-1]
        yield start, end
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def raw_episode_index(episodes, settings):
    plan = EpisodeIndex()
    for number, (user, assistant) in enumerate(episodes):
        chunks = []
        for role, text in (("User", user), ("Assistant", assistant)):
            for start, end in chunk_ranges(text, settings.raw_chunk_chars, settings.raw_chunk_overlap_chars):
                index = len(plan.texts)
                plan.texts.append(role + ": " + text[start:end])
                chunks.append(ChunkInput(role, start, end, index))
        if chunks:
            plan.chunks[number] = chunks
            plan.representatives[number] = chunks[0].vector_index
    return plan


def render_chunks(selected, parents):
    """Keep session relevance, original round order, and User-before-Assistant order."""
    grouped, sessions = {}, OrderedDict()
    for row in selected:
        eid = row["episode_id"]
        grouped.setdefault(eid, {}).setdefault(row["role"], []).append((row["start"], row["end"]))
        sessions.setdefault(parents[eid].session_id, set()).add(eid)
    parts, episodes = [], []
    ordered = [eid for ids in sessions.values() for eid in sorted(ids,
        key=lambda eid: (parents[eid].round_idx, parents[eid].at, eid))]
    for eid in ordered:
        episode = parents[eid]
        excerpts = []
        for role in ("User", "Assistant"):
            merged = []
            for start, end in sorted(grouped[eid].get(role, [])):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            original = episode.user_text if role == "User" else episode.assistant_text
            for start, end in merged:
                excerpts.append("%s [chars %d:%d of %d]:\n%s" % (role, start, end, len(original), original[start:end]))
        text = "\n\n".join(excerpts)
        episodes.append((episode, text))
        parts.append("[%s] recorded=%s session=%s round=%d\n%s" % (
            episode.key, episode.at.isoformat(), episode.session_id, episode.round_idx + 1, text))
    heading = "Retrieved original conversation excerpts (sessions by relevance; rounds in original order):\n"
    return (heading + "\n\n".join(parts) if parts else ""), episodes


def _covered(row, supplied):
    end = row["start"]
    spans = sorted((r["start"], r["end"]) for r in supplied
                   if r["episode_id"] == row["episode_id"] and r["role"] == row["role"])
    for start, stop in spans:
        if start > end:
            break
        end = max(end, stop)
        if end >= row["end"]:
            return True
    return False


def companion_spans(selected, parents, max_chars):
    """Use already-loaded source text; neighbors are distinct from retrieved anchors."""
    companions, seen = [], set()
    for row in selected:
        eid = row["episode_id"]
        if row["role"] != "Assistant" or eid in seen:
            continue
        seen.add(eid)
        episode = parents[eid]
        text = episode.user_text
        if not text.strip():
            continue
        end = min(len(text), max_chars)
        neighbor = dict(key="%s:User:0:%d" % (episode.key, end), episode_id=eid,
            source=episode.key, role="User", start=0, end=end, text=text[:end],
            session_id=episode.session_id, round_idx=episode.round_idx,
            source_chars=len(text), prefix_limited=end < len(text), reason="same_round_user_context")
        if not _covered(neighbor, selected):
            companions.append(neighbor)
    return companions


def fuse_ranks(rows, rrf_k):
    """Give initial retrieval and BGE ranks equal votes, without mixing raw logits."""
    if rows and all("rerank_score" in row for row in rows):
        for rank, row in enumerate(sorted(rows, key=lambda r: (
                -r["rerank_score"], r["retrieval_rank"])), 1):
            row["rerank_rank"] = rank
        for row in rows:
            row["rank_fusion_score"] = 1.0 / (rrf_k + row["retrieval_rank"]) + 1.0 / (rrf_k + row["rerank_rank"])
    else:
        for row in rows:
            row["rank_fusion_score"] = row["fusion_score"]


def choose_spans(rows, parents, settings, counter, budget):
    """Soft diversity, source-preserving overlap removal, and complete-body packing."""
    remaining, chosen = list(rows), []
    words = {row["key"]: set(terms(row["text"])) for row in rows}
    while remaining and len(chosen) < settings.max_raw_chunks:
        for row in remaining:
            a = words[row["key"]]
            overlap = max((len(a & words[old["key"]]) / len(a | words[old["key"]])
                           if a | words[old["key"]] else 0.0 for old in chosen), default=0.0)
            row["redundancy"] = overlap
            row["selection_score"] = row["rank_fusion_score"] / (1.0 + overlap)
        row = min(remaining, key=lambda r: (-r["selection_score"], r["retrieval_rank"]))
        remaining.remove(row)
        supplied = chosen + companion_spans(chosen, parents, settings.raw_chunk_chars)
        if _covered(row, supplied):
            row["omission_reason"] = "already_in_returned_spans"
            continue
        trial = chosen + [row]
        companions = companion_spans(trial, parents, settings.raw_chunk_chars)
        memory, _ = render_chunks(trial + companions, parents)
        if counter.count(memory) <= budget:
            row["selection_rank"] = len(chosen) + 1
            chosen.append(row)
        else:
            row["omission_reason"] = "context_budget"
    for row in remaining:
        row["omission_reason"] = "top_k"
    return chosen, companion_spans(chosen, parents, settings.raw_chunk_chars)


def _span_metadata(row):
    return {k: v for k, v in row.items() if k != "text"} | dict(
        text_sha256=hashlib.sha256(row["text"].encode()).hexdigest())


def retrieve_chunks(store, models, settings, user_id, question, at, tags):
    """Dense+lexical chunk recall, bounded BGE ranking, then literal excerpt return."""
    counter = budget_counter(settings)
    budget = settings.context_budget_tokens
    store.check_index_fingerprint(settings.index_fingerprint(models.embedding_name), legacy=True)
    store.check_embedding_model(models.embedding_name)
    vectors = models.embedder.embed([question], purpose="embed_query", tags=tags)
    if len(vectors) != 1:
        raise ValueError("raw chunk retrieval requires one query embedding")
    statuses = SEARCHABLE_WITH_ARCHIVE["episode"]
    streams = [("dense", store.vector_search("episode_chunk", normalize(vectors[0]), statuses, settings.dense_k)),
               ("lexical", store.fts_search("episode_chunk", question, statuses, settings.sparse_k))]
    fused = {}
    for channel, rows in streams:
        for position, (cid, score) in enumerate(rows, 1):
            row = fused.setdefault(cid, dict(id=cid, key="C%d" % cid, fusion_score=0.0, via=[]))
            row["fusion_score"] += 1.0 / (settings.rrf_k + position)
            row["via"].append(channel)
            row[channel + "_rank"] = position
            row[channel + "_score"] = score
    check_read_budget()
    chunk_by_id = {c.id: c for c in store.episode_chunks(fused)}
    parents = {e.id: e for e in store.episodes({c.episode_id for c in chunk_by_id.values()}, statuses)}
    ranked = []
    for cid, row in fused.items():
        chunk = chunk_by_id.get(cid)
        if chunk is None or chunk.episode_id not in parents:
            continue
        episode = parents[chunk.episode_id]
        original = episode.user_text if chunk.role == "User" else episode.assistant_text
        row.update(episode_id=episode.id, source=episode.key, role=chunk.role, start=chunk.start, end=chunk.end,
                   text=original[chunk.start:chunk.end], session_id=episode.session_id, round_idx=episode.round_idx)
        ranked.append(row)
    ranked.sort(key=lambda row: (-row["fusion_score"], row["session_id"], row["round_idx"], row["role"], row["start"]))
    for rank, row in enumerate(ranked, 1):
        row["retrieval_rank"] = rank
    top = ranked[:settings.rerank_candidates]
    rank_trace = dict(backend="fusion", requested_backend=settings.rerank_backend,
                      eligible_candidates=len(ranked), scored_candidates=0)
    retrieval_errors = {}
    if top and settings.rerank_backend in ("bge", "remote"):
        from .reranking import RerankDocument, RerankFailure, local_reranker
        documents = [RerankDocument(row["key"], row["text"],
            "[%s] source=%s recorded=%s role=%s\n" % (row["key"], row["source"],
                parents[row["episode_id"]].at.isoformat(), row["role"])) for row in top]
        try:
            if settings.rerank_backend == "remote":
                from .remote_rerank import RemoteReranker
                ranker = RemoteReranker(settings)
            else:
                ranker = local_reranker(settings)
            scores, trace = ranker.rank(question, documents)
            if set(scores) != {row["key"] for row in top}:
                raise RerankFailure("raw chunk reranker omitted candidates")
            for row in top:
                row["rerank_score"] = scores[row["key"]]
            rank_trace = dict(trace, requested_backend=settings.rerank_backend,
                              eligible_candidates=len(ranked), scored_candidates=len(top))
        except RerankFailure as exc:
            retrieval_errors["rerank"] = str(exc)
            rank_trace.update(error=str(exc), degraded=True)
            if settings.rerank_strict:
                raise
    elif top and settings.rerank_backend != "fusion":
        raise ValueError("raw_chunks supports bge, remote or fusion ranking without generation")
    check_read_budget()
    fuse_ranks(top, settings.rrf_k)
    for row in ranked[len(top):]:
        row["omission_reason"] = "rerank_candidate_cap"
    chosen, companions = choose_spans(top, parents, settings, counter, budget)
    memory, episodes = render_chunks(chosen + companions, parents)
    check_read_budget()
    selection = Selection([], episodes, counter.count(memory))
    coverage = dict(scope="bounded original-text chunk retrieval", search_complete=False,
                    context_complete=False, top_k=settings.max_raw_chunks, candidates=len(ranked))
    selection.coverage = coverage
    rows = [_span_metadata(row) for row in chosen]
    neighbor_rows = [_span_metadata(row) for row in companions]
    trace = dict(user_id=user_id, at=at.isoformat(), message=question, tier=models.tier,
        plan=dict(queries=[question], keywords=[], time_range=None, needs_all=False, focus="any",
                  source="direct_raw_chunks", operation="lookup"),
        candidates=[{k: v for k, v in row.items() if k != "text"} for row in ranked],
        used_archive=any(e.status == "archived" for e, _ in episodes),
        reranked=bool(top and rank_trace.get("scored_candidates")), reranking=rank_trace,
        retrieval_errors=retrieval_errors, selected=dict(facts=[], episodes=[e.key for e, _ in episodes],
                                                        chunks=[row["key"] for row in chosen]),
        omitted=dict(facts=[], episodes=[], chunks=[row["key"] for row in ranked if row not in chosen]),
        memory=memory, memory_tokens=counter.count(memory), context_tokens=counter.count(memory) + counter.count(question),
        effective_budget=budget, budget=budget, history_tokens=0, coverage=coverage, retrieval_coverage=coverage,
        budget_counter=counter.manifest(), revision=store.get_meta("revision"), backend="qdrant",
        recall_mode="raw_chunks", relations={}, coverage_supplement=dict(attempted=False),
        evidence_selection=dict(scope="original_chunks", policy="retrieval_bge_rank_fusion_then_diversity", top_k=settings.max_raw_chunks),
        sources={row["key"]: [row["source"]] for row in chosen + companions},
        raw_chunks=dict(selected=rows, companions=neighbor_rows, candidate_count=len(ranked), max_chars=settings.raw_chunk_chars,
                        overlap_chars=settings.raw_chunk_overlap_chars, top_k=settings.max_raw_chunks,
                        companion_span_chars=sum(row["end"]-row["start"] for row in companions),
                        companion_policy="same_round_user_prefix_one_chunk_length", round_order="within_session"))
    return trace, selection, memory, SearchResult([], {}, used_archive=trace["used_archive"],
                                                coverage=coverage, rerank_trace=rank_trace)
