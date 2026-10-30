"""Agent memory API: record, organize and retrieve evidence; generation lives in agent_adapter."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import prompts
from .memory_pages import evidence_pages
from .budget import budget_counter
from .config import AppConfig, MemorySettings, Models
from .indexing import episode_index, stored_chunks
from .lifecycle import LifecycleReport, run_lifecycle
from .llm import FunctionChat, FunctionEmbedder, ModelError, UsageLedger
from .read import (Candidate, QueryPlan, Retriever, SearchResult, Selection, effective_budget, fit_to_budget,
                   heuristic_plan, plan_query)
from .read_budget import (ReadBudget, check_read_budget, current_read_budget, is_read_service_error, read_scope)
from .schema import ARCHIVED, EPISODE_SKIPPED, EPISODE_WITHHELD, FACT_CURRENT, FACT_SUPERSEDED, FORGOTTEN, SEARCHABLE, SEARCHABLE_WITH_ARCHIVE, Fact
from .store import Store, WriteConflict
from .qdrant import Qdrant, QdrantSettings
from .textutil import estimate_tokens, truncate_tokens
from .timeutil import format_day, from_iso, to_iso
from .vecmath import normalize
from .write import WriteReport, Writer

_KEY = re.compile(r"^\s*([FfEe])(\d+)\s*$")


@dataclass(frozen=True)
class SessionHandle:
    user_id: str
    session_id: str


@dataclass
class Reply:
    text: str
    notes: str
    trace: Dict[str, Any]


@dataclass
class Recall:
    """recall() 的结果。context 整段放进外部模型的系统提示词即可；memory / rules 分开给，方便自己排版。"""
    context: str                  # 记忆块 + 使用规则
    memory: str                   # 只有记忆：按时间排好的事实和原文片段
    rules: str                    # 使用规则：冲突取新、天数怎么用、没有就说不知道……
    items: List[Dict[str, Any]]   # 选中的条目：key、kind、date、status、text
    trace: Dict[str, Any]
    trace_id: Optional[str] = None


def _safe_name(user_id: str) -> str:
    """把用户名或题号变成安全的文件名（字母、数字、汉字等原样保留）。被替换过字符的名字后面加一段哈希，
    免得"a/b"和"a_b"共用一个记忆库。"""
    name = re.sub(r"[^\w.-]+", "_", user_id).strip(".") or "default"
    if name != user_id:
        name += "-" + hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:8]
    return name


class MemorySystem:
    def __init__(self, models: Models, settings: MemorySettings, data_dir: Optional[str] = None,
                 trace_dir: Optional[str] = None, wal: bool = False, *, storage=None, store_factory=None):
        """data_dir stores local traces only; online memory always uses a Qdrant server."""
        self.storage = storage or QdrantSettings.environment()
        self.store_factory = store_factory or Store
        self._qdrant_client = Qdrant(self.storage) if store_factory is None else None
        self.models = models
        self.settings = settings
        self.data_dir = os.path.expanduser(data_dir) if data_dir else data_dir
        self.trace_dir = os.path.expanduser(trace_dir) if trace_dir else trace_dir
        self.wal = wal
        self._stores: Dict[str, Store] = {}
        self._stores_lock = threading.Lock()
        for directory in (self.data_dir, self.trace_dir):
            if directory:
                os.makedirs(directory, exist_ok=True)

    @classmethod
    def open(cls, data_dir: Optional[str], llm: Optional[Any] = None, embed: Optional[Any] = None, *,
             answer: Optional[Any] = None, embedding_name: Optional[str] = None,
             config: Optional[AppConfig] = None, settings: Optional[MemorySettings] = None,
             discover: bool = True, ledger: Optional[UsageLedger] = None,
             trace_dir: Optional[str] = None) -> "MemorySystem":
        """给 Python 写的 agent 用的入口。

        llm：记忆模型，传函数 fn(messages) → 文本，或任何有 chat() 的对象；embed：fn(texts) → 向量列表。
        两个都可以不传：discover=True 时再去环境变量和本机 Ollama 找，还是没有就降级运行（见 Models.tier）。
        embed 是函数时请给一个稳定的 embedding_name，换了 embedding 才能被识别出来、提醒 reindex。
        """
        from .discover import HostModels, resolve_models
        ledger = ledger if ledger is not None else UsageLedger()

        def chat_model(x: Any) -> Any:
            if x is None or hasattr(x, "chat"):
                return x
            return FunctionChat(x, name=getattr(x, "__name__", "function"), ledger=ledger)

        def embed_model(x: Any) -> Any:
            if x is None or hasattr(x, "embed"):
                return x
            return FunctionEmbedder(x, name=getattr(x, "__name__", "function"))

        memory_model, answer_model = chat_model(llm), chat_model(answer)
        names = {}
        if memory_model is not None:
            names["memory"] = getattr(memory_model, "name", "caller")
        if answer_model is not None or memory_model is not None:
            names["chat"] = getattr(answer_model or memory_model, "name", "caller")
        embedder = embed_model(embed)
        if embedder is not None:
            embedding_name = embedding_name or "caller:%s" % getattr(embedder, "name", "embed")
            names["embedding"] = embedding_name
        host = HostModels(chat=answer_model or memory_model, memory=memory_model, embedder=embedder,
                          embedding_name=embedding_name, names=names, source="caller")
        models = resolve_models(config, ledger, host=host, env=None if discover else {}, probe=discover)
        return cls(models, settings or (config.memory if config else MemorySettings()), data_dir=data_dir,
                   trace_dir=trace_dir, storage=config.storage if config else None)

    def store(self, user_id: str) -> Store:
        with self._stores_lock:
            existing = self._stores.get(user_id)
        if existing is not None:
            return existing
        # Network initialization must not block cached stores for other users.
        created = self.store_factory(user_id=user_id, settings=self.storage,
                    max_bytes=self.settings.max_storage_bytes,
                    **({"client": self._qdrant_client} if self._qdrant_client else {}))
        with self._stores_lock:
            existing = self._stores.setdefault(user_id, created)
        if existing is not created:
            created.close()
        return existing

    def close(self) -> None:
        with self._stores_lock:
            for store in self._stores.values():
                store.close()
            self._stores.clear()

    # ------------------------------------------------------------ sessions
    def open_session(self, user_id: str, at: datetime, external_id: Optional[str] = None) -> SessionHandle:
        return SessionHandle(user_id, self.store(user_id).create_session(at, external_id))

    def add_turn(self, session: SessionHandle, role: str, content: str, at: datetime, idempotency_key=None) -> int:
        """原文立刻写进日志；整理成记忆要等会话结束（或累计够轮数）。"""
        if role not in ("user", "assistant"):
            raise ValueError(f"unknown role {role!r}")
        return self.store(session.user_id).append_turn(session.session_id, role, content, at, idempotency_key=idempotency_key)

    def close_session(self, session: SessionHandle, at: datetime,
                      tags: Optional[Mapping[str, str]] = None) -> Tuple[WriteReport, LifecycleReport]:
        store = self.store(session.user_id)
        report = Writer(store, self.models, self.settings, tags).consolidate(session.session_id, closed_at=at)
        return report, run_lifecycle(store, at, self.settings)

    def maintain(self, user_id: str, at: datetime) -> LifecycleReport:
        """单独跑一次巩固与遗忘。会话结束时会自动跑；时间流逝本身（模拟时钟推进、隔天再打开）也该跑一次。"""
        return run_lifecycle(self.store(user_id), at, self.settings)

    def rebuild(self, user_id: str) -> List[WriteReport]:
        """按时间顺序补抽降级或抽取不完整的轮次，成功后才移出队列。"""
        return Writer(self.store(user_id), self.models, self.settings).backfill()

    def recover(self, user_id: str) -> List[WriteReport]:
        """启动时重试持久补抽队列，再整理上次没正常关闭的会话。"""
        store = self.store(user_id)
        reports = []
        # 已关闭会话也可能因不完整输出留下补抽队列；启动恢复时每轮只尝试一次。
        if store.backlog_count() and self.models.for_purpose("extract") is not None:
            reports.extend(Writer(store, self.models, self.settings).backfill())
        for session_id in store.open_sessions():
            turns = store.turns(session_id)
            closed_at = from_iso(turns[-1]["at"]) if turns else from_iso(store.session(session_id)["opened_at"])
            reports.append(Writer(store, self.models, self.settings).consolidate(session_id, closed_at=closed_at))
        return reports

    # ------------------------------------------------------------- recall
    def recall(self, user_id: str, query: str, at: datetime, budget: Optional[int] = None,
               tags: Optional[Mapping[str, str]] = None, save_trace: bool = True,
               coverage_budget: Optional[int] = None) -> Recall:
        """只取记忆，不生成回答：给外部 agent 用。budget 是这次给记忆的 token 预算，不传用配置里的。"""
        if coverage_budget is not None and (isinstance(coverage_budget, bool) or
                not isinstance(coverage_budget, int) or coverage_budget < 0):
            raise ValueError('coverage_budget must be a nonnegative integer or null')
        settings = self.settings if budget is None else dataclasses.replace(
            self.settings, context_budget_tokens=max(int(budget), 200), max_history_share=0)
        store = self.store(user_id)
        with read_scope(ReadBudget(settings.read_timeout_s, settings.read_retries_per_call,
                                   settings.read_retries_total, settings.read_retry_wait_s)):
            trace, selection, memory, _ = self._retrieve(store, user_id, query, at, settings, dict(tags or {}), coverage_budget)
        trace["mode"] = "recall"
        try:
            self._touch(store, selection, at)
        except WriteConflict:
            trace["access_update"] = "deferred_due_to_concurrent_write"
        rules = prompts.MEMORY_RULES
        context = prompts.MEMORY_CONTEXT.format(today=format_day(at), memory=memory, rules=rules)
        from .fact_content import fact_text
        items = [{"key": f.key, "kind": "fact", "date": f.when().label(), "status": f.status,
                  "replaced_by": "F%d" % f.superseded_by if f.superseded_by else None, "text": fact_text(f),
                  "sentence": f.text, "value": f.value, "subject": f.subject, "attribute": f.attribute,
                  "memory_kind": f.kind, "semantics": f.semantics,
                  "relations": f.relations,
                  "recorded_at": f.recorded_at.isoformat(), "event_date": f.event.label() if f.event else None,
                  "event_status": f.semantics.get('event_status', 'unknown'),
                  "sources": trace["sources"].get(f.key, []),
                  "confidence": f.confidence, "uncertain": f.uncertain, "session_id": f.session_id,
                  "recorded_at": to_iso(f.recorded_at),
                  "valid_from": f.valid_from.isoformat() if f.valid_from else None,
                  "valid_to": f.valid_to.isoformat() if f.valid_to else None}
                 for f in selection.facts]
        items += [{"key": e.key, "kind": "episode", "date": e.at.date().isoformat(), "status": e.status,
                   "replaced_by": None, "text": text, "session_id": e.session_id,
                   "speakers": ["user", "assistant"]} for e, text in selection.episodes]
        trace_id = None
        if save_trace and self.trace_dir:
            trace_id = self._save_trace(trace, (tags or {}).get("question_id") or "%s-%s" % (
                _safe_name(user_id), at.strftime("%Y%m%dT%H%M%S")), unique=not (tags or {}).get("question_id"))
        return Recall(context, memory, rules, items, trace, trace_id)

    def _retrieve(self, store, user_id, message, at, s, tags, coverage_budget=None):
        with store.read_snapshot():
            check_read_budget()
            retriever = Retriever(store, self.models, s, tags, today=at.date())
            plan = plan_query(self.models, message, at, s, tags)
            check_read_budget()
            result = retriever.search(plan)
            check_read_budget()
            result = retriever.rerank(result, message, at, plan=plan)
            check_read_budget()
            counter = budget_counter(s)
            history_size = store.history_tokens(counter)
            budget = effective_budget(s, history_size, counter.count(message))
            selection = retriever.select(result, plan, budget, today=at.date())
            supplement = dict(attempted=False, initial_budget=budget, initial_selected=len(selection.facts))
            if (plan.needs_all and not s.include_sources and not selection.coverage.get('context_complete')
                    and coverage_budget is not None and int(coverage_budget) > 0):
                # Reuse the same bounded, already retrieved fact pool and rerank.
                # No extra generation, original-text read or answer-dependent repair.
                # Bytes and fact count are independent bounds. A host may have
                # enough bytes already, or a smaller host cap must not undo the
                # initial selection that was explicitly authorized by budget.
                expanded_budget = max(budget, min(int(coverage_budget), s.answer_input_budget_tokens))
                fact_limit = max(s.max_facts_all, min(s.answer_fact_limit, s.aggregate_contribution_limit))
                if expanded_budget > budget or fact_limit > s.max_facts_all:
                    budget = expanded_budget
                    packed_settings = dataclasses.replace(s, max_facts_all=fact_limit)
                    packed = Retriever(store, self.models, packed_settings, tags, today=at.date())
                    selection = packed.select(result, plan, budget, today=at.date())
                    supplement.update(attempted=True, final_budget=budget, final_selected=len(selection.facts),
                                      fact_limit=fact_limit,
                                      complete=selection.coverage.get('context_complete', False))
            memory = fit_to_budget(selection, at.date(), budget, s.tentative_threshold, counter=counter)
            warnings = []
            ingestion = store.record('extraction_health', 'current') or {}
            if ingestion.get('unresolved_batches', 0):
                selection.coverage['source_protocol_complete'] = False
                if plan.needs_all:
                    selection.coverage['context_complete'] = False
                warnings.append('Some source details were not retained as usable memory. Provided facts remain usable, '
                    'but source coverage is incomplete. Preserve supported answers and explain missing premises; '
                    'do not claim an exhaustive list or exact final total when missing details could change it. '
                    'Describe the information gap without exposing internal processing details.')
            if plan.needs_all and not selection.coverage.get("context_complete"):
                warnings.append("Coverage incomplete: only a known subset is available; do not claim an exhaustive list or final total.")
            if result.relation_trace.get('ambiguous_aliases'):
                warnings.append('Entity-name ambiguity: ' + json.dumps(result.relation_trace['ambiguous_aliases'], ensure_ascii=False) +
                                ' matches multiple stored entities; use qualifying evidence and do not silently merge them.')
            if warnings:
                warning = '\n'.join(warnings) + '\n'
                memory = warning + fit_to_budget(selection, at.date(), max(0, budget-counter.count(warning)),
                                                s.tentative_threshold, counter=counter)
                memory = counter.truncate(memory, budget)
            trace = dict(user_id=user_id, at=to_iso(at), message=message, tier=self.models.tier,
                plan=plan.to_dict(), candidates=[self._candidate_row(c, result) for c in result.ranked[:30]],
                used_archive=result.used_archive, reranked=result.reranked, reranking=result.rerank_trace,
                retrieval_errors={k: v for k, v in (("query", plan.error), ("rerank", result.rerank_error)) if v},
                selected=dict(facts=[f.key for f in selection.facts], episodes=[e.key for e, _ in selection.episodes]),
                omitted=dict(facts=selection.omitted_facts, episodes=selection.omitted_episodes),
                memory_tokens=counter.count(memory), effective_budget=budget, memory=memory,
                budget=s.context_budget_tokens, context_tokens=counter.count(memory)+counter.count(message), history_tokens=history_size,
                coverage=dict(selection.coverage), retrieval_coverage=dict(result.coverage),
                budget_counter=counter.manifest(), revision=store.get_meta("revision"), backend="qdrant",
                recall_mode=s.recall_mode,
                relations=dict(result.relation_trace), coverage_supplement=supplement,
                evidence_selection=dict(selection.ordering),
                ingestion_quality=dict(ingestion),
                sources={f.key: ["E%d" % eid for eid in store.sources([f.id])[f.id]] for f in selection.facts})
            return trace, selection, memory, result

    def recall_pages(self, user_id: str, query: str, at: datetime):
        """外部 agent 的聚合分页入口；每页有界，返回覆盖信息，不自动拼入单个提示词。"""
        store = self.store(user_id)
        plan = plan_query(self.models, query, at, self.settings)
        plan.needs_all = True
        result = Retriever(store, self.models, self.settings, today=at.date()).search(plan)
        origins = {"F%d" % key: set(ids) for key, ids in store.sources(
            c.id for c in result.ranked if c.kind == "fact").items()}
        pages = evidence_pages(result, self.settings, origins)
        return {"coverage": result.coverage, "budget_counter": budget_counter(self.settings).manifest(),
                "pages": pages[:self.settings.aggregate_max_pages],
                "pages_complete": bool(result.coverage.get("search_complete"))
                                  and len(pages) <= self.settings.aggregate_max_pages}

    @staticmethod
    def _touch(store: Store, selection: Selection, at: datetime) -> None:
        store.touch("fact", [f.id for f in selection.facts], at)
        store.touch("episode", [e.id for e, _ in selection.episodes], at)

    def respond(self, user_id, message, at, session=None, tags=None, **kwargs):
        """Compatibility wrapper for the optional host adapter. No answer review or rewriting."""
        from .agent_adapter import AgentAdapter
        return AgentAdapter(self).respond(user_id, message, at, session=session, tags=tags, **kwargs)

    @staticmethod
    def _size(store: Store) -> Dict[str, int]:
        """参与默认检索的记忆条数，报告里用来说明检索规模。"""
        counts = store.counts()
        return {kind: sum(counts[kind].get(status, 0) for status in SEARCHABLE[kind]) for kind in SEARCHABLE}

    def _save_trace(self, trace: Dict[str, Any], name: str, unique: bool) -> str:
        """评测时一题一个文件（重答就覆盖）；对话时同一时刻的多次回答不能互相覆盖
        （模拟时钟不推进时时间都一样），重名时加序号。返回 trace 编号（文件名去掉 .json）。"""
        base = os.path.join(self.trace_dir, _safe_name(name))
        path, n = base + ".json", 1
        while unique and os.path.exists(path):
            n += 1
            path = "%s-%d.json" % (base, n)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(trace, fh, ensure_ascii=False, indent=1)
        return os.path.basename(path)[:-len(".json")]

    @staticmethod
    def _candidate_row(cand, result: SearchResult) -> Dict[str, Any]:
        item = result.items[cand.key]
        preview = item.text if isinstance(item, Fact) else item.text()
        if not isinstance(item, Fact) and cand.chunks:
            chunk = cand.chunks[0][0]
            original = item.user_text if chunk.role == "User" else item.assistant_text
            preview = chunk.role + ": " + original[chunk.start:chunk.end]
        return {"key": cand.key, "score": round(cand.score, 5), "sim": round(cand.best_sim, 3),
                "bm25": cand.bm25_rank, "via": cand.via, "status": item.status,
                "confidence": round(item.confidence, 2) if isinstance(item, Fact) else None,
                "preview": preview[:120].replace("\n", " "),
                "rerank_score": cand.rerank_score,
                "matched_chunks": [{"role": c.role, "start": c.start, "end": c.end,
                                    "ordinal": c.ordinal, "sim": round(sim, 3)} for c, sim in cand.chunks]}

    def _conversation(self, store: Store, session_id: str) -> str:
        window = self.prepare_window(store, store.conversation_turns(session_id))
        return '\n'.join(('User' if r['role'] == 'user' else 'Assistant') + ': ' + r['content'] for r in window.messages)

    def prepare_window(self, store, history, tags=None):
        from .working_memory import select_window, ensure_eviction_ready
        return ensure_eviction_ready(self, store, select_window(history, self.settings), tags)

    def answer_memory_budget(self, system_prompt, history, at):
        """Available fact-text units after reserving actual host framing and context."""
        counter = budget_counter(self.settings)
        empty = prompts.MEMORY_CONTEXT.format(today=format_day(at), memory='', rules=prompts.MEMORY_RULES)
        messages = [dict(role='system', content=system_prompt + '\n\n<retrieved_memory>\n' + empty + '\n</retrieved_memory>')]
        messages += [{k: row[k] for k in ('role', 'content')} for row in history]
        available = self.settings.answer_input_budget_tokens - counter.messages(messages) - 128
        if available < 200:
            raise ModelError('latest input and working memory leave no safe fact-context budget')
        return available

    def _maybe_flush(self, store: Store, session: SessionHandle, tags: Mapping[str, str]) -> Optional[str]:
        row = store.session(session.session_id)
        pending_turns = len(store.turns(session.session_id, row["turns_done"]))
        if pending_turns < 2 * self.settings.flush_rounds:
            return None
        report = Writer(store, self.models, self.settings, tags).consolidate(session.session_id)
        return report.summary()

    # ------------------------------------------------------------- inspect
    def search(self, user_id: str, query: str) -> SearchResult:
        """不调用模型的检索（启发式查询计划），给 /mem 搜索用。"""
        plan: QueryPlan = heuristic_plan(query)
        return Retriever(self.store(user_id), self.models, self.settings).search(plan)

    def facts(self, user_id: str, include_inactive: bool = False) -> List[Fact]:
        statuses = (FACT_CURRENT, FACT_SUPERSEDED, ARCHIVED) if include_inactive else (FACT_CURRENT,)
        return self.store(user_id).facts(statuses=statuses)

    def history(self, user_id: str, fact_key: str) -> List[Fact]:
        kind, item_id = self._parse_key(fact_key)
        if kind != "fact":
            raise ValueError("version history exists for facts (F…) only")
        return [dataclasses.replace(f, text="", value="", semantics={}) if f.status == FORGOTTEN else f
                for f in self.store(user_id).chain(item_id)]

    def forget(self, user_id: str, key: str) -> List[str]:
        """遗忘一条记忆（软删除），返回连同被遗忘的所有条目编号，见 Store.forget。"""
        kind, item_id = self._parse_key(key)
        return self.store(user_id).forget(kind, item_id)

    def pending(self, user_id: str) -> List[Dict[str, Any]]:
        store = self.store(user_id)
        out = []
        for row in store.pending():
            new, old = store.facts([row["new_fact_id"]]), store.facts([row["old_fact_id"]])
            if new and old:
                out.append({"id": row["id"], "new": new[0], "old": old[0], "reason": row["reason"]})
        return out

    def confirm(self, user_id: str, pending_id: int, accept: bool) -> str:
        """处理一条待确认的冲突：accept 表示新事实取代旧事实，否则两条都保留。"""
        store = self.store(user_id)
        with store.transaction():
            row = store.resolve_pending(pending_id, "accepted" if accept else "rejected")
            new = store.fact(row["new_fact_id"])
            store.clear_uncertain(new.id)
            old_id = row["old_fact_id"]
            if accept:
                store.set_confidence(new.id, 1.0)      # 用户亲口确认过
                # 等待确认期间旧事实可能已经被别的新事实取代：接到版本链当前的末端，不能把链剪断
                head = store.chain(old_id)[-1]
                if head.id == new.id:
                    return "F%d 已经是 F%d 的新版本" % (new.id, old_id)
                store.supersede(head.id, new.id, new.valid_from)
                old_id = head.id
        return "F%d %s F%d" % (new.id, "取代了" if accept else "与", old_id) + ("" if accept else " 并存")

    def stats(self, user_id: str) -> Dict[str, Any]:
        store = self.store(user_id)
        return {"counts": store.counts(), "history_tokens": store.history_tokens(counter=budget_counter(self.settings)),
                "pending": len(store.pending()), "embedding_model": store.get_meta("embedding_model"),
                "backlog": store.backlog_count(), "tier": self.models.tier, "index_coverage": store.index_coverage(),
                "storage": store.storage_usage(), "budget_counter": budget_counter(self.settings).manifest()}

    def reindex(self, user_id: str) -> int:
        """换了 embedding 模型后重建向量：原文和事实都在，随时可以重算。"""
        store = self.store(user_id)
        source = store.index_source_fingerprint()
        facts = store.facts(statuses=(FACT_CURRENT, FACT_SUPERSEDED, ARCHIVED))
        episodes = [e for e in store.episodes() if e.status not in (EPISODE_SKIPPED, EPISODE_WITHHELD, FORGOTTEN)]
        fingerprint = self.settings.index_fingerprint(self.models.embedding_name)
        if store.get_meta("index_fingerprint") == fingerprint and store.get_meta("index_source_sha256") == source:
            store.validate_index()
            return len(facts) + len(episodes)
        index = episode_index([(e.user_text, e.assistant_text) for e in episodes], self.settings)
        # 分块变动复用同一向量空间的事实向量；不调用事实抽取，也不重算无变化的事实。
        old_ids, old_vectors = store.vectors("fact", (FACT_CURRENT, FACT_SUPERSEDED, ARCHIVED))
        previous_manifest = json.loads(store.get_meta("index_manifest") or "{}")
        from .fact_content import fact_text, index_signature
        signature = index_signature()
        reusable = dict(zip(old_ids, old_vectors)) if (store.get_meta("embedding_model") == self.models.embedding_name and
            previous_manifest.get("embedding_revision","unknown") == self.settings.embedding_model_revision and
            all(previous_manifest.get(k)==v for k,v in signature.items())) else {}
        missing = [f for f in facts if f.id not in reusable]
        texts = [fact_text(f) for f in missing] + index.texts
        # 先把新向量全部算好，再一次性替换：中途失败时旧索引原封不动
        vectors = [normalize(v) for v in self.models.embedder.embed(texts, purpose="embed")] if texts else []
        if len(vectors) != len(texts):
            raise ModelError("sent %d texts, got %d embeddings back" % (len(texts), len(vectors)))
        store.replace_vectors(self.models.embedding_name, {
            "fact": [(f.id, reusable[f.id]) for f in facts if f.id in reusable] + list(zip([f.id for f in missing], vectors[:len(missing)])),
            "episode": [(episodes[n].id, vectors[len(missing) + offset])
                        for n, offset in index.representatives.items()]},
            chunks={episodes[n].id: stored_chunks(index, n, vectors, len(missing)) for n in index.chunks},
            expected_source=source,
            manifest={"fingerprint":fingerprint, "strategy":"legacy",
                      **signature,
                      "embedding_revision":self.settings.embedding_model_revision,
                      "counter":budget_counter(self.settings).manifest(), "format":2})
        return len(facts) + len(episodes)

    def rollback_index(self, user_id: str, generation=None) -> int:
        return self.store(user_id).rollback_index(generation)

    @staticmethod
    def _parse_key(key: str) -> Tuple[str, int]:
        match = _KEY.match(key)
        if not match:
            raise ValueError(f"expected an id like F12 or E5, got {key!r}")
        return ("fact" if match.group(1).upper() == "F" else "episode"), int(match.group(2))
