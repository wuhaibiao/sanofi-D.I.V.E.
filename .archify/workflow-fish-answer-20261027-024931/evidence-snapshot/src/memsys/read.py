"""读取路径：查询分析 → 混合检索（向量 + BM25 + 时间）→ 融合与扩展 → 固定预算内组装工作记忆。

最终回答由宿主 Agent 生成；默认只返回事实，原文召回需要显式诊断模式。
"""

from __future__ import annotations

import re
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from . import prompts
from .budget import TokenCounter, budget_counter
from .config import MemorySettings, Models
from .fact_content import fact_text
from .memory_compaction import shared_value_key
from .llm import BadJSONError, ModelError, chat_json
from .operation_validation import validate_operation
from .queryops import OPERATIONS, complete_search, is_duration_total, operation_for
from .schema import ARCHIVED, FACT_SUPERSEDED, FORGOTTEN, SEARCHABLE, SEARCHABLE_WITH_ARCHIVE, Episode, EpisodeChunk, Fact
from .store import Store
from .textutil import estimate_tokens, fts_query, split_tokens, terms, truncate_tokens
from .timeutil import DateSpan, describe_offset, format_day, parse_span
from .vecmath import normalize, scores

_COUNTING = re.compile(r"\b(how many|how much|total|in total|list|all the|every|how often|times)\b|"
                       r"多少|几[个次件本天]|一共|总共|列出|所有", re.IGNORECASE)
_CURRENT = re.compile(r"\b(now|currently|current|still|these days|at the moment)\b|现在|目前|如今", re.IGNORECASE)
_PAST = re.compile(r"\b(previous|previously|before|used to|formerly|old)\b|以前|之前|原来", re.IGNORECASE)
_ANSWER = re.compile(r"ANSWER\s*[:：]", re.IGNORECASE)
_NOTES = re.compile(r"^\s*NOTES\s*[:：]\s*", re.IGNORECASE)
_ORDINAL_WORDS = {word: index for index, word in enumerate(
    'first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth '
    'thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth'.split(), 1)}


def _ordinal_ranges(text: str, question: str) -> List[Tuple[int, int]]:
    """Locate literal numbered entries requested by an ordinal, without renumbering bullets."""
    numbers = {int(match.group(1)) for match in re.finditer(r'\b(\d{1,3})(?:st|nd|rd|th)\b', question, re.I)}
    numbers.update(int(match.group(1)) for match in re.finditer(r'第\s*(\d{1,3})', question))
    numbers.update(_ORDINAL_WORDS[word.lower()] for word in re.findall(
        r'\b(?:' + '|'.join(_ORDINAL_WORDS) + r')\b', question, re.I))
    if not numbers:
        return []
    entries = list(re.finditer(r'(?m)^[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*)?(\d{1,3})[.)、:：](?!\d)[ \t]*', text))
    return [(entry.start(), entries[index + 1].start() if index + 1 < len(entries) else len(text))
            for index, entry in enumerate(entries) if int(entry.group(1)) in numbers]


@dataclass
class QueryPlan:
    queries: List[str]
    keywords: List[str]
    time_range: Optional[DateSpan]
    needs_all: bool
    focus: str          # current / past / any
    source: str         # llm / heuristic / fallback
    error: Optional[str] = None
    operation: str = "lookup"
    qualifiers: List[str] = field(default_factory=list)
    trigger: str = ""
    operation_validation: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["time_range"] = ([self.time_range.start.isoformat(), self.time_range.end.isoformat()]
                              if self.time_range else None)
        return data


def _strings(value: object) -> List[str]:
    """模型应该给字符串列表，偶尔给成单个字符串或夹着别的类型：统一成去空的字符串列表。"""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if isinstance(v, (str, int, float)) and str(v).strip()]


def _flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def question_qualifiers(question, values):
    """A mandatory qualifier must not narrow a coordinated question to one arm."""
    valid = []
    lower = question.casefold()
    for value in _strings(values):
        target = value.casefold()
        if target not in lower:
            continue
        starts = [m.start() for m in re.finditer(re.escape(target), lower)]
        if any(re.match(r"\s*(?:and|or)\b|\s*[和及或、]", lower[n+len(target):]) or
               re.search(r"\b(?:and|or)\s*$|[和及或、]\s*$", lower[:n]) for n in starts):
            continue
        if value not in valid:
            valid.append(value)
    return valid[:8]


def heuristic_plan(question: str, source: str = "heuristic", semantic: bool = True) -> QueryPlan:
    focus = "current" if _CURRENT.search(question) else "past" if _PAST.search(question) else "any"
    operation = operation_for(question)
    return QueryPlan([question], terms(question)[:12], None,
                     complete_search(operation) if semantic else bool(_COUNTING.search(question)), focus, source,
                     operation=operation)


def plan_query(models: Models, question: str, today: datetime, settings: MemorySettings,
               tags: Optional[Mapping[str, str]] = None, *, force: bool = False) -> QueryPlan:
    """让记忆模型把问题拆成检索计划；没有模型（L0）或模型输出不可用时退回启发式计划。"""
    planner = models.for_purpose("query")
    from .read_budget import current_read_budget
    from .read_retry import ReadTransportChat
    read_budget = current_read_budget()
    if planner is not None and read_budget is not None:
        planner = ReadTransportChat(planner, read_budget)
    if not settings.query_analysis or settings.query_policy == "off" or planner is None:
        direct = heuristic_plan(question, semantic=settings.operation_semantics)
        direct.trigger = "disabled_or_no_planner"
        return direct
    direct = heuristic_plan(question, semantic=settings.operation_semantics)
    if settings.query_policy == "on_risk" and not force and not query_risks(question, direct):
        direct.source = "direct"
        direct.trigger = "simple_lookup"
        return direct
    user = prompts.QUERY_USER.format(today=format_day(today), question=question)
    if settings.query_expansion:
        user += prompts.QUERY_EXPANSION
    messages = [{"role": "system", "content": prompts.QUERY_SYSTEM}, {"role": "user", "content": user}]
    if budget_counter(settings).messages(messages) > settings.memory_input_budget_tokens:
        return heuristic_plan(question, source="budget_fallback", semantic=settings.operation_semantics)
    try:
        data = chat_json(planner, messages, purpose="query", max_tokens=1200 if settings.query_expansion else 1000,
                         tags=tags)
    except BadJSONError as exc:
        plan = heuristic_plan(question, source="fallback", semantic=settings.operation_semantics)
        plan.error = str(exc)[:500]
        return plan
    except ModelError as exc:
        if exc.account_problem or getattr(exc, 'read_state', None) in ('timeout', 'cancelled'):
            raise
        if read_budget is not None:
            read_budget.degraded('query', exc)
        plan = heuristic_plan(question, source="service_fallback", semantic=settings.operation_semantics)
        plan.error = str(exc)[:500]
        return plan
    if not isinstance(data, dict):
        return heuristic_plan(question, source="fallback", semantic=settings.operation_semantics)
    queries = [question]
    for q in _strings(data.get("queries")):
        if q not in queries:
            queries.append(q)
    keywords = _strings(data.get("keywords"))
    time_range = None
    raw_range = data.get("time_range")
    if isinstance(raw_range, dict):
        start, end = parse_span(raw_range.get("start")), parse_span(raw_range.get("end"))
        if start or end:
            start, end = start or end, end or start
            time_range = DateSpan(min(start.start, end.start), max(start.end, end.end), "day")
    focus = str(data.get("focus") or "any").lower()
    operation = str(data.get("operation") or "")
    if operation not in OPERATIONS:
        operation = operation_for(question)
    fallback = operation_for(question)
    if is_duration_total(question):
        operation = 'sum_events'
    if fallback in ("ratio", "difference") or fallback in ("compare_states", "date_difference", "latest_state") and operation in ("sum_events", "count_entities"):
        operation = fallback
    if fallback == "latest_state" and operation == "lookup":
        operation = fallback
    if fallback == "historical_state" and operation in ("lookup", "latest_state"):
        operation = fallback
    correction = {}
    if settings.operation_semantics:
        operation, correction = validate_operation(question, operation, fallback)
    qualifiers = question_qualifiers(question, data.get("qualifiers"))
    return QueryPlan(queries[:settings.max_queries + 1], keywords[:16], time_range,
                     _flag(data.get("needs_all")) or settings.operation_semantics and complete_search(operation),
                     focus if focus in ("current", "past", "any") else "any", "llm",
                     operation=operation, qualifiers=qualifiers, operation_validation=correction,
                     trigger="low_recall_confidence" if force else "complex_question" if settings.query_policy == "on_risk" else "always")


def query_risks(question, plan):
    reasons = []
    if plan.needs_all or plan.operation != "lookup":
        reasons.append("complete_or_semantic_operation")
    if plan.focus != "any" or re.search(r"\b(?:when|date|before|after|since|until|during|latest|last|first|other|same|then|they|their|older|initially|previous)\b|何时|日期|之前|之后|当时|后来|它|那个|又", question, re.I):
        reasons.append("time_or_dependency")
    return reasons


@dataclass
class Candidate:
    kind: str                     # fact / episode
    id: int
    score: float = 0.0
    best_sim: float = 0.0
    bm25_rank: Optional[int] = None
    via: List[str] = field(default_factory=list)
    chunks: List[Tuple[EpisodeChunk, float]] = field(default_factory=list)
    rerank_score: Optional[float] = None

    @property
    def key(self) -> str:
        return ("F%d" if self.kind == "fact" else "E%d") % self.id


Item = Union[Fact, Episode]


@dataclass
class SearchResult:
    ranked: List[Candidate]
    items: Dict[str, Item]
    used_archive: bool
    reranked: bool = False
    coverage: Dict[str, object] = field(default_factory=dict)
    rerank_error: Optional[str] = None
    rerank_trace: Dict[str, object] = field(default_factory=dict)
    relation_trace: Dict[str, object] = field(default_factory=dict)


@dataclass
class Selection:
    facts: List[Fact]
    episodes: List[Tuple[Episode, str]]    # (轮次, 放进上下文的文本，可能被截断)
    context_tokens: int
    omitted_facts: List[str] = field(default_factory=list)
    omitted_episodes: List[str] = field(default_factory=list)
    coverage: Dict[str, object] = field(default_factory=dict)
    ordering: Dict[str, object] = field(default_factory=dict)
    selection_trace: Dict[str, object] = field(default_factory=dict)


class Retriever:
    def __init__(self, store: Store, models: Models, settings: MemorySettings,
                 tags: Optional[Mapping[str, str]] = None, today: Optional[date] = None):
        self.store = store
        self.today = today or date.today()
        self.models = models
        self.settings = settings
        self.tags = dict(tags or {})
        self.tokens = budget_counter(settings)

    def search(self, plan: QueryPlan) -> SearchResult:
        query_cache: Dict[str, List[List[float]]] = {}
        if plan.needs_all:
            result = self._search(plan, SEARCHABLE_WITH_ARCHIVE, query_cache)
            result.used_archive = any(item.status == ARCHIVED for item in result.items.values())
            return result
        result = self._search(plan, SEARCHABLE, query_cache)
        needs_archive = (plan.needs_all or plan.focus == "past" or plan.time_range is not None or
                         len(result.ranked) < self.settings.fallback_min_candidates)
        counts = self.store.counts() if needs_archive else {}
        kinds = ("fact", "episode") if self.settings.include_sources else ("fact",)
        if needs_archive and any(counts.get(kind, {}).get(ARCHIVED, 0) for kind in kinds):
            # 相关记忆太少时，去已归档的记忆里兜底：遗忘是降级，不是删除
            fallback = self._search(plan, SEARCHABLE_WITH_ARCHIVE, query_cache)
            if {c.key for c in fallback.ranked} - {c.key for c in result.ranked}:
                fallback.used_archive = True
                return fallback
        return result

    def _search(self, plan: QueryPlan, statuses: Mapping[str, Sequence[str]],
                query_cache: Optional[Dict[str, List[List[float]]]] = None) -> SearchResult:
        s = self.settings
        searchable_kinds = ("fact", "episode") if s.include_sources else ("fact",)
        scope = ("all searchable facts and originals in this tenant; relevance is not guaranteed"
                 if s.include_sources else "all searchable facts in this tenant; relevance is not guaranteed")
        manifest = json.loads(self.store.get_meta("index_manifest") or "{}")
        # 读取旧索引用它原来的分块计数身份；本次回答预算可独立选择 UTF-8。
        legacy_counter = manifest.get("counter")
        self.store.check_index_fingerprint(s.index_fingerprint(self.models.embedding_name, legacy_counter=legacy_counter),
                                          legacy=True)
        enumerate_pool = False
        if plan.needs_all:
            counts = self.store.counts()
            pool = sum(sum(counts.get(kind, {}).get(status, 0) for status in statuses[kind])
                       for kind in searchable_kinds)
            enumerate_pool = pool <= s.aggregate_candidate_limit
        found: Dict[str, Candidate] = {}

        def hit(kind: str, item_id: int) -> Candidate:
            key = ("F%d" if kind == "fact" else "E%d") % item_id
            if key not in found:
                found[key] = Candidate(kind, item_id)
            return found[key]

        query_vectors = []
        if self.store.get_meta("embedding_model"):
            self.store.check_embedding_model(self.models.embedding_name)
            cache = query_cache if query_cache is not None else {}
            if "vectors" not in cache:
                cache["vectors"] = [normalize(v) for v in self.models.embedder.embed(
                    plan.queries, purpose="embed_query", tags=self.tags)]
            query_vectors = cache["vectors"]
        lexical_query = " ".join(list(plan.keywords) + list(plan.queries))
        kinds = ("fact",)
        if s.include_sources:
            kinds = ("fact", "episode", "episode_chunk") if s.episode_chunk_vectors else ("fact", "episode")
        for kind in kinds:
            eligible = statuses["episode"] if kind == "episode_chunk" else statuses[kind]
            streams = [("dense", self.store.vector_search(kind, vector, eligible, s.dense_k)) for vector in query_vectors]
            streams.append(("lexical", self.store.fts_search(kind, lexical_query, eligible, s.sparse_k)))
            for source, rows in streams:
                chunks = {c.id: c for c in self.store.episode_chunks(i for i, _ in rows)} if kind == "episode_chunk" else {}
                # One contribution per episode per search stream, regardless of chunk count.
                contributed = set()
                for rank, (item_id, score) in enumerate(rows):
                    chunk = chunks.get(item_id)
                    if kind == "episode_chunk" and chunk is None:
                        continue
                    cand = hit("episode" if chunk else kind, chunk.episode_id if chunk else item_id)
                    if source == "dense":
                        cand.best_sim = max(cand.best_sim, score)
                        if chunk and score >= s.min_similarity:
                            cand.chunks.append((chunk, score))
                    else:
                        cand.bm25_rank = min(cand.bm25_rank or rank+1, rank+1)
                    if cand.key not in contributed:
                        cand.score += (max(0, score) if source == "dense" else 1) / (s.rrf_k + rank + 1)
                        contributed.add(cand.key)
                    channel = source + ("_chunks" if chunk else "")
                    if channel not in cand.via:
                        cand.via.append(channel)
        for cand in found.values():
            cand.chunks = sorted(cand.chunks, key=lambda pair: (-pair[1], pair[0].ordinal))[:4]
        kept = {k: c for k, c in found.items() if c.best_sim >= s.min_similarity or c.bm25_rank is not None}
        items = self._load(kept, statuses)
        kept = {k: c for k, c in kept.items() if k in items}
        if plan.needs_all and enumerate_pool:
            # Exhaustive source enumeration is distinct from bounded ANN retrieval.
            all_items = self.store.facts(statuses=statuses["fact"])
            if s.include_sources:
                all_items += self.store.episodes(statuses=statuses["episode"])
            for item in all_items:
                items[item.key] = item
                if item.key not in kept:
                    kept[item.key] = Candidate("fact" if isinstance(item, Fact) else "episode", item.id,
                                               via=["source_enumeration"])

        # 显式原文诊断模式才扩展来源轮次；默认事实模式不读取来源正文。
        # 关联证据取最强者，直接与关联命中也取较强者，避免同一信息的两种视图重复加分。
        fact_scores = {c.id: c.score for c in kept.values() if c.kind == "fact"}
        bonus: Dict[str, float] = {}
        for fact_id, episode_ids in (self.store.sources(fact_scores).items() if s.include_sources else ()):
            for episode_id in episode_ids:
                key = "E%d" % episode_id
                bonus[key] = max(bonus.get(key, 0.0), s.expand_weight * fact_scores[fact_id])
        episode_scores = {c.id: c.score for c in kept.values() if c.kind == "episode"}
        for episode_id, fact_ids in self.store.facts_of_episodes(episode_scores).items():
            for fact_id in fact_ids:
                key = "F%d" % fact_id
                bonus[key] = max(bonus.get(key, 0.0), 0.5 * s.expand_weight * episode_scores[episode_id])
        for key, extra in bonus.items():
            cand = kept.get(key) or Candidate("fact" if key[0] == "F" else "episode", int(key[1:]))
            cand.score = max(cand.score, extra)
            if "expand" not in cand.via:
                cand.via.append("expand")
            kept[key] = cand
        items.update(self._load({k: c for k, c in kept.items() if k not in items}, statuses))
        kept = {k: c for k, c in kept.items() if k in items}

        relation_trace = {'enabled': s.relations_enabled, 'added_candidates': 0}
        if s.relations_enabled and s.relation_hops:
            from .relational_memory import RelationIndex
            seeds = [items[c.key] for c in sorted(kept.values(), key=lambda c: (-c.score, c.key))
                     if c.kind == 'fact'][:s.relation_seed_facts]
            related, relation_trace = RelationIndex(self.store).expand(' '.join(plan.queries), seeds,
                statuses['fact'], plan, self.today, s)
            added = 0
            seed_score = max((c.score for c in kept.values() if c.kind == 'fact'), default=1 / (s.rrf_k + 1))
            for fid, path in related.items():
                fact = path['fact']
                if fact.key not in kept:
                    kept[fact.key] = Candidate('fact', fid)
                    added += 1
                cand = kept[fact.key]
                cand.score = max(cand.score, seed_score * (0.8 ** path['hop']))
                if 'relation' not in cand.via:
                    cand.via.append('relation')
                items[fact.key] = fact
            relation_trace['added_candidates'] = added

        for key, cand in kept.items():
            item = items[key]
            span = item.when() if isinstance(item, Fact) else DateSpan.day(item.at.date())
            if plan.time_range is not None:
                in_range = span.overlaps(plan.time_range, s.time_slack_days)
                cand.score *= s.time_boost if in_range else s.time_penalty
            if isinstance(item, Fact) and (item.status == FACT_SUPERSEDED or item.superseded_by):
                cand.score *= s.superseded_weight if plan.focus == "current" else 1.2 if plan.focus == "past" else 1.0
            if isinstance(item, Fact):
                # 置信度低（只是打算、拿不准、存疑）的事实略微降权，见 update.py
                cand.score *= 1.0 - s.confidence_weight * (1.0 - item.confidence)
        ranked = sorted(kept.values(), key=lambda c: (-c.score, c.key))
        coverage = {}
        if plan.needs_all:
            # 覆盖声明针对当前检索规则下的候选，不把语义相关性判断称作真值。
            cap = s.aggregate_candidate_limit
            coverage = {"scope": scope,
                        "eligible_pool": pool, "candidates": len(ranked),
                        "search_complete": enumerate_pool and len(ranked) <= cap,
                        "enumeration_complete": enumerate_pool,
                        "limit": cap, "needs_all": True}
            ranked = ranked[:cap]
            items = {c.key: items[c.key] for c in ranked}
        return SearchResult(ranked, items, used_archive=False, coverage=coverage, relation_trace=relation_trace)

    def _apply_recall_mode(self, result: SearchResult) -> None:
        """Enforce the facts boundary even for a caller-supplied candidate result."""
        if self.settings.include_sources:
            return
        ranked = [c for c in result.ranked if c.kind == "fact" and isinstance(result.items.get(c.key), Fact)]
        if len(ranked) != len(result.ranked) and result.coverage:
            result.coverage = {**result.coverage, "search_complete": False,
                               "scope": "searchable facts only; input included excluded originals",
                               "candidates": len(ranked)}
        result.ranked = ranked
        result.items = {c.key: result.items[c.key] for c in ranked}

    def rerank(self, result: SearchResult, question: str, today: datetime,
               plan: Optional[QueryPlan] = None) -> SearchResult:
        """让 memory 模型从前 N 个候选里挑出回答需要的条目，挑中的排到最前面。

        模型输出不可用时保持原顺序。没被挑中的条目仍按原分数排在后面，预算有余时还能用上。
        """
        s = self.settings
        self._apply_recall_mode(result)
        result.rerank_trace = {"requested_backend": s.rerank_backend, "backend": "fusion"}
        if s.rerank_backend == "fusion" or not result.ranked:
            result.rerank_trace["reason"] = "configured_fusion" if s.rerank_backend == "fusion" else "no_candidates"
            return result
        if s.rerank_backend in ("bge", "remote"):
            from .reranking import RerankDocument, RerankFailure, local_reranker
            # 重排工作量有界；未打分条目仍在融合排名的尾部，完整分页不会丢失任何候选。
            scope_order={}
            if plan is not None and not s.include_sources:
                from .fact_selection import order_facts
                result.ranked,scope_order=order_facts(result,plan)
            top = result.ranked[:s.rerank_candidates]
            documents = []
            for cand in top:
                item = result.items[cand.key]
                if isinstance(item, Fact):
                    header = "[%s] object=%s.%s event=%s recorded=%s status=%s replaced_by=%s\n" % (
                        item.key,item.subject,item.attribute,item.when().label(),item.recorded_at.date(),item.status,item.superseded_by)
                    documents.append(RerankDocument(cand.key,fact_text(item),header))
                else:
                    # 以真实角色完整原文分窗；每个窗口保留日期，助手窗口再保留原始问题。
                    for role, text in (("User",item.user_text),("Assistant",item.assistant_text)):
                        if not text:
                            continue
                        # 命中窗口及有界邻域；无该角色命中时遍历其完整原文。
                        spans = [(max(c.start-160,0),min(c.end+160,len(text))) for c,_ in cand.chunks if c.role==role]
                        if not spans:
                            spans = [(0,len(text))]
                        merged = []
                        for left,right in sorted(spans):
                            if merged and left<=merged[-1][1]:
                                merged[-1]=(merged[-1][0],max(right,merged[-1][1]))
                            else:
                                merged.append((left,right))
                        for left,right in merged:
                            documents.append(RerankDocument(cand.key,text[left:right],"[%s] recorded=%s\nRole=%s\n" % (
                                item.key,item.at.date().isoformat(),role), item.user_text if role == "Assistant" else "", left,role))
            try:
                if s.rerank_backend == "remote":
                    from .remote_rerank import RemoteReranker
                    ranker = RemoteReranker(s)
                else:
                    ranker = local_reranker(s)
                scores, trace = ranker.rank(question,documents)
                if set(scores) != {c.key for c in top}:
                    raise RerankFailure("reranker omitted candidates")
                for cand in top:
                    cand.rerank_score = scores[cand.key]
                    cand.via.append("bge_rerank")
                chosen = sorted(top,key=lambda c:(-scores[c.key],-c.score,c.key))
                chosen_keys = {c.key for c in chosen}
                result.ranked = chosen + [c for c in result.ranked if c.key not in chosen_keys]
                result.reranked = True
                result.rerank_trace = {**trace, "requested_backend":s.rerank_backend, "scored_candidates":len(top),
                    "eligible_candidates":len(result.ranked), "full_candidate_set_retained":True,
                    "candidate_scope":scope_order}
            except RerankFailure as exc:
                result.rerank_error = str(exc)
                result.rerank_trace.update(error=str(exc), strict=s.rerank_strict, degraded=True)
                if s.rerank_strict:
                    raise
            return result
        reranker = self.models.for_purpose("rerank")
        from .read_budget import current_read_budget
        from .read_retry import ReadTransportChat
        read_budget = current_read_budget()
        if reranker is not None and read_budget is not None:
            reranker = ReadTransportChat(reranker, read_budget)
        if not s.llm_rerank or not result.ranked or reranker is None:
            result.rerank_trace["reason"] = "llm_disabled_or_unavailable"
            return result
        top = result.ranked[:s.rerank_candidates]
        lines = []
        for cand in top:
            item = result.items[cand.key]
            if isinstance(item, Fact):
                status = " · outdated" if item.superseded_by else ""
                lines.append("[%s] %s%s · %s" % (cand.key, item.when().label(), status, fact_text(item)))
            else:
                if s.query_focused_excerpts:
                    text = episode_excerpt(item, plan or heuristic_plan(question), 128, cand.chunks).replace("\n", " ")
                else:
                    text = "User: %s" % item.user_text[:300].replace("\n", " ")
                    if item.assistant_text:
                        text += " … Assistant: %s" % item.assistant_text[:150].replace("\n", " ")
                lines.append("[%s] %s · %s" % (cand.key, item.at.date().isoformat(), text))
        messages = [{"role": "system", "content": prompts.RERANK_SYSTEM},
                    {"role": "user", "content": prompts.RERANK_USER.format(
                        today=format_day(today), question=question, entries="\n".join(lines))}]
        if self.tokens.messages(messages) > s.memory_input_budget_tokens:
            result.rerank_trace["reason"] = "llm_input_budget_exceeded"
            return result
        try:
            data = chat_json(reranker, messages, purpose="rerank", max_tokens=600, tags=self.tags)
        except BadJSONError as exc:
            result.rerank_error = str(exc)[:500]
            result.rerank_trace["reason"] = "invalid_llm_json"
            return result
        except ModelError as exc:
            if exc.account_problem or getattr(exc, 'read_state', None) in ('timeout', 'cancelled'):
                raise
            if read_budget is not None:
                read_budget.degraded('rerank', exc)
            result.rerank_error = str(exc)[:500]
            return result
        picked = data.get("relevant") if isinstance(data, dict) else data
        if not isinstance(picked, list):
            return result
        valid = {c.key: c for c in top}
        order: List[str] = []
        for key in picked:
            key = str(key).strip().upper()
            if key in valid and key not in order:
                order.append(key)
        for rank, key in enumerate(order):
            valid[key].via.append("rerank#%d" % (rank + 1))
        chosen = [valid[k] for k in order]
        rest = [c for c in result.ranked if c.key not in set(order)]
        result.ranked = chosen + rest
        result.reranked = True
        result.rerank_trace.update(backend="llm", selected=order)
        return result

    def _load(self, cands: Mapping[str, Candidate], statuses: Mapping[str, Sequence[str]]) -> Dict[str, Item]:
        fact_ids = [c.id for c in cands.values() if c.kind == "fact"]
        episode_ids = [c.id for c in cands.values() if c.kind == "episode"]
        items: Dict[str, Item] = {}
        for fact in self.store.facts(fact_ids, statuses["fact"]):
            items[fact.key] = fact
        if episode_ids:
            for episode in self.store.episodes(episode_ids, statuses["episode"]):
                items[episode.key] = episode
        return items

    def select(self, result: SearchResult, plan: QueryPlan, budget: Optional[int] = None,
               today: Optional[date] = None) -> Selection:
        """在预算内挑选事实；显式原文诊断模式才使用剩余预算放来源轮次。
        budget 由 effective_budget 算出；不给就用配置里的固定预算。"""
        s = self.settings
        today = today or max((item.when().start if isinstance(item, Fact) else item.at.date()
                              for item in result.items.values()), default=date(2000, 1, 1))
        budget = max(0, s.context_budget_tokens if budget is None else budget)
        self._apply_recall_mode(result)
        if s.selection_policy == "coverage" and s.include_sources:
            from .evidence_selection import select_evidence
            return select_evidence(self, result, plan, budget, today)
        fact_budget = (int(budget * (s.fact_budget_share_all if plan.needs_all else s.fact_budget_share))
                       if s.include_sources else budget)
        max_facts = s.max_facts_all if plan.needs_all else s.max_facts
        selection = Selection([], [], 0, coverage=dict(result.coverage))
        chosen = set()

        def latest(fact: Fact) -> Fact:
            seen = {fact.id}
            while fact.superseded_by and fact.superseded_by not in seen:
                seen.add(fact.superseded_by)
                found = self.store.facts([fact.superseded_by])
                if not found:
                    break
                fact = found[0]
            return fact

        fact_order = result.ranked
        if not s.include_sources:
            from .fact_selection import order_facts
            fact_order,selection.ordering=order_facts(result,plan)
        elif plan.operation == 'advice':
            # Only reorder already retrieved candidates. Preserve relevance order
            # within each group; never import new memories or original messages.
            fact_order = sorted(result.ranked, key=lambda cand: not (
                cand.kind == 'fact' and result.items[cand.key].kind != 'assistant'))
        for cand in fact_order:
            if cand.kind != "fact":
                continue
            fact = result.items[cand.key]
            current = latest(fact)
            # 当前问题只需链末端；历史问题保留命中的旧版本及最新版本，不展开整条链。
            # 最新版本已遗忘时不能把旧值重新解释为当前状态。
            bundle = ([current] if plan.focus == "current" else [fact, current])
            bundle = list({f.id: f for f in bundle if f.status != FORGOTTEN}.values())
            if plan.focus == "current" and (current.status == FORGOTTEN or current.superseded_by):
                bundle = []
            added = [f for f in bundle if f.id not in chosen]
            options = [added]
            if len(added) > 1:
                preferred = fact if plan.focus == "past" else current
                options.append([preferred] if preferred.id not in chosen and preferred.status != FORGOTTEN else [])
            for option in options:
                if not option:
                    continue
                trial = Selection(selection.facts + option, [], 0, ordering=selection.ordering)
                if len(trial.facts) <= max_facts and self.tokens.count(
                        render_memory(trial, today, s.tentative_threshold)) <= fact_budget:
                    selection.facts.extend(option)
                    chosen.update(f.id for f in option)
                    break

        selection.omitted_facts = [c.key for c in result.ranked if c.kind == "fact" and c.id not in chosen]

        used = self.tokens.count(render_memory(selection, today, s.tentative_threshold)) if selection.facts else 0
        for cand in result.ranked:
            if cand.kind != "episode":
                continue
            if len(selection.episodes) >= s.max_episodes:
                selection.omitted_episodes.append(cand.key)
                continue
            episode = result.items[cand.key]
            refreshed = self.store.episodes([episode.id], SEARCHABLE_WITH_ARCHIVE['episode'])
            if not refreshed:
                selection.omitted_episodes.append(cand.key)
                continue
            episode = refreshed[0]
            result.items[cand.key] = episode
            selection.episodes.append((episode, ""))
            overhead = self.tokens.count(render_memory(selection, today, s.tentative_threshold))
            selection.episodes.pop()
            cap = min(s.episode_max_tokens, max(budget - overhead - 1, 0))
            text = (episode_excerpt(episode, plan, cap, cand.chunks, counter=self.tokens) if s.query_focused_excerpts
                    else self.tokens.truncate(episode.text(), cap))
            if not text:
                selection.omitted_episodes.append(cand.key)
                continue
            selection.episodes.append((episode, text))
            used = self.tokens.count(render_memory(selection, today, s.tentative_threshold))
        selection.context_tokens = used
        if plan.needs_all:
            selection.coverage["context_complete"] = (bool(result.coverage.get("search_complete"))
                and bool(result.ranked)
                and not selection.omitted_facts and not selection.omitted_episodes
                and all(text == episode.text() for episode, text in selection.episodes))
        return selection


def episode_excerpt(episode: Episode, plan: QueryPlan, limit: int,
                    matches: Sequence[Tuple[EpisodeChunk, float]] = (),
                    counter: Optional[TokenCounter] = None) -> str:
    """预算内选择原文窗口；保留说话人和原始顺序，明确标记省略。"""
    counter = counter or TokenCounter("estimate")
    full = episode.text()
    if counter.count(full) <= limit:
        return full
    marker = "[excerpt; other text omitted]\n"
    available = limit - counter.count(marker) - 8
    if available < 12:
        return counter.truncate(full, limit)
    query_terms = set(terms(" ".join(plan.keywords + plan.queries)))
    windows, positions = [], []
    ordinal_ranges = {}
    for role, text in (("User", episode.user_text), ("Assistant", episode.assistant_text)):
        ordinal_ranges[role] = _ordinal_ranges(text, plan.queries[0] if plan.queries else '')
        # 在命中片段的边界切开，尾部短片段不会混入一个很长的前缀窗口。
        boundaries = sorted({0, len(text)} | {offset for chunk, _ in matches if chunk.role == role
                                             for offset in (chunk.start, chunk.end) if 0 <= offset <= len(text)}
                            | {offset for span in ordinal_ranges[role] for offset in span})
        for start, end in zip(boundaries, boundaries[1:]):
            offset = start
            for piece in counter.split(text[start:end], max(available // 3, 4)):
                windows.append((role, piece))
                positions.append((role, offset, offset + len(piece)))
                offset += len(piece)
    lexical = [set(terms(piece)) & query_terms for _, piece in windows]
    counts = {word: sum(word in found for found in lexical) for word in query_terms}
    lexical_scores = [sum(1.0 / counts[word] for word in found) for found in lexical]
    semantic = [max((sim for chunk, sim in matches if chunk.role == role and
                     start < chunk.end and end > chunk.start), default=0.0) for role, start, end in positions]
    ordinal = [any(start < end_ and end > start_ for start_, end_ in ordinal_ranges[role])
               for role, start, end in positions]
    relevant = [i for i in range(len(windows)) if ordinal[i] or lexical_scores[i] > 0 or semantic[i] > 0]
    if not relevant:
        return counter.truncate(full, limit)
    ranked = sorted(relevant, key=lambda i: (not ordinal[i], -semantic[i], -lexical_scores[i], i))
    # 先放命中窗口，再用邻近原文补足上下文；窗口始终按原文顺序呈现。
    neighbors = sorted((i for i in range(len(windows)) if i not in relevant),
                       key=lambda i: (min(abs(i - j) for j in relevant), i))
    chosen: List[int] = []

    def render(indices: List[int]) -> str:
        return marker + "\n".join("%s: %s" % windows[i] for i in sorted(indices))

    for index in ranked + neighbors:
        if counter.count(render(chosen + [index])) <= limit:
            chosen.append(index)
    return render(chosen) if chosen else counter.truncate(full, limit)


def effective_budget(s: MemorySettings, history_tokens: Optional[int], question_tokens: int = 0) -> int:
    """这次能给记忆的 token：固定预算，和"全部历史 × max_history_share − 问题本身"取小，但不低于 min_context_tokens。

    历史很短时，送进上下文的记忆加上问题不会比历史本身还长：不能变相把整段历史塞进去（手册 F.1）。"""
    budget = s.context_budget_tokens
    if history_tokens is not None and s.max_history_share > 0:
        budget = min(budget, max(int(history_tokens * s.max_history_share) - question_tokens, s.min_context_tokens))
    return budget


def fit_to_budget(selection: Selection, today: date, limit: int, tentative_threshold: float = 0.6,
                  counter: Optional[TokenCounter] = None) -> str:
    """以最终渲染文本核验预算。事实保持完整，超限时按检索优先级移除条目。"""
    limit = max(limit, 0)
    counter = counter or TokenCounter("estimate")
    memory = render_memory(selection, today, tentative_threshold)
    while counter.count(memory) > limit and selection.episodes:
        episode, _ = selection.episodes.pop()
        selection.omitted_episodes.append(episode.key)
        memory = render_memory(selection, today, tentative_threshold)
    while counter.count(memory) > limit and selection.facts:
        fact = selection.facts.pop()
        selection.omitted_facts.append(fact.key)
        memory = render_memory(selection, today, tentative_threshold)
    if counter.count(memory) > limit:
        memory = counter.truncate(memory, limit)
    if selection.coverage and (selection.omitted_facts or selection.omitted_episodes):
        selection.coverage["context_complete"] = False
    selection.context_tokens = counter.count(memory)
    if selection.selection_trace:
        from .evidence_selection import refresh_trace
        refresh_trace(selection)
    return memory


def render_memory(selection: Selection, today: date, tentative_threshold: float = 0.6) -> str:
    """把选中的记忆排成时间线。每条都带上绝对日期和代码算好的"距今多少天"；
    已被取代的标 replaced by，存疑的标 unconfirmed，置信度低的标 tentative。"""
    lines: List[str] = []
    if selection.facts:
        personal_first = (selection.ordering.get('scope') == 'user'
                          and selection.ordering.get('operation') == 'advice')
        if personal_first:
            from .task_context import ADVICE_CONTEXT, advice_sections
            lines.append(ADVICE_CONTEXT.rstrip())
            groups = advice_sections(selection.facts)
        else:
            groups = [("## Remembered facts (oldest first)", sorted(selection.facts, key=lambda f: (f.when().start, f.id)))]
        shared_values = {}
        for title, facts in groups:
            lines.append(title)
            for fact in facts:
                lines.append(_render_fact(fact, today, tentative_threshold, shared_values, personal_first))
    if selection.episodes:
        if lines:
            lines.append("")
        lines.append("## Past conversation excerpts (oldest first)")
        for episode, text in sorted(selection.episodes, key=lambda e: (e[0].at, e[0].id)):
            day = DateSpan.day(episode.at.date())
            lines.append("[%s] conversation on %s (%s)" % (episode.key, day.label(), describe_offset(day, today)))
            lines.append(text)
            lines.append("")
    if not lines:
        reason = ("relevant memories did not fit the budget" if selection.omitted_facts or selection.omitted_episodes
                  else "no relevant memories were found")
        lines.append("## Memory\n(%s)" % reason)
    return "\n".join(lines).rstrip()


def _render_fact(fact, today, tentative_threshold, shared_values, task_context=False):
    parts = []
    if fact.event:
        label = 'planned for' if fact.semantics.get('event_status') == 'planned' else 'event date'
        parts.append("%s %s (%s)" % (label, fact.event.label(), describe_offset(fact.event, today)))
    told = DateSpan.day(fact.recorded_at.date())
    parts.append("noted %s (%s)" % (told.label(), describe_offset(told, today)))
    parts.append('memory_kind=' + fact.kind)
    if task_context and fact.semantics.get('context_role'):
        parts.append('context_role=' + fact.semantics['context_role'])
    if fact.superseded_by:
        parts.append("replaced by F%d" % fact.superseded_by)
    elif fact.uncertain:
        parts.append("unconfirmed update relationship; verify original statement")
    elif fact.confidence < tentative_threshold:
        parts.append("tentative")
    if fact.semantics.get("value_type") not in (None, "unknown", "other"):
        parts.append("quantity_type=" + str(fact.semantics["value_type"]))
    if fact.semantics.get("object_key"):
        parts.append("scope=" + str(fact.semantics["object_key"]))
    if fact.semantics.get('event_key'):
        parts.append('event_identity=' + json.dumps(fact.semantics['event_key'],ensure_ascii=False))
    if fact.semantics.get("qualifiers"):
        parts.append("conditions=" + ",".join(fact.semantics["qualifiers"]))
    if fact.semantics.get('relative_time'):
        parts.append('relative to noted date: ' + json.dumps(fact.semantics['relative_time'], ensure_ascii=False))
    key = shared_value_key(fact)
    reference = shared_values.get(key) if key is not None else None
    text = "[%s] %s · %s" % (fact.key, " · ".join(parts), fact_text(fact, value_reference=reference))
    if key is not None:
        shared_values.setdefault(key, fact.key)
    return text


def parse_answer(text: str) -> Tuple[str, str]:
    """把模型输出拆成（笔记, 回答）。没有 ANSWER 标记时整段都当回答。"""
    matches = list(_ANSWER.finditer(text))
    if not matches:
        return "", _NOTES.sub("", text).strip()
    last = matches[-1]
    notes = _NOTES.sub("", text[:last.start()]).strip()
    answer = text[last.end():].strip()
    return notes, answer or text.strip()
