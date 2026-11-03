"""写入路径：会话结束时（或对话中累计够轮数时）把原始轮次整理进记忆。

切轮次 → 检索相关旧记忆 → 单次事实抽取 → 精确文本去重与新增 → 建索引。

所有模型调用都在写库之前完成，最后在一个事务里一次写入并推进"已整理到第几句"的水位线，
所以中途任何一步失败，重跑都不会产生重复或半截的记忆。

没有记忆模型时（L0）照样切轮次、门控、建索引，只是不抽事实：这些轮次记进 extract_backlog，
之后有了模型由 backfill（启动恢复 / memsys rebuild）补抽与去重新增。
不可解析或截断的整批输出仍报错；可解析输出中的异常条目隔离，正常事实一次提交。
异常正文和具体原因单独留档，不成为可检索事实，也不代表语义覆盖完整。
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import prompts
from .budget import budget_counter
from .config import MemorySettings, Models
from .fact_content import fact_identity, fact_text, index_signature, validate_value
from .structure_memory import assistant_structures
from .relational_memory import RelationIndex, ground_relations, normalize_relations, merge_relations
from .textutil import terms
from .indexing import EpisodeIndex, episode_index, stored_chunks
from .llm import IncompleteJSONError, ModelError, parse_json
from .schema import EPISODE_ACTIVE, EPISODE_SKIPPED, FACT_CURRENT, FACT_KINDS, Episode, normalize_semantics
from .store import Store, WriteConflict
from .textutil import slug
from .timeutil import DateSpan, format_stamp, from_iso, parse_span
from .update import MemoryInserter, certainty_to_confidence
from .vecmath import normalize

_SMALL_TALK = re.compile(
    r"^(hi|hello|hey|thanks?( you)?( so much)?|thank you|ok(ay)?|great|cool|nice|got it|sounds good|bye|"
    r"good (morning|night)|你好|谢谢|好的|嗯|再见|收到)[\s.!。！~]*$", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(i|i'm|i've|i'd|my|me|mine|we|our|us)\b|我", re.IGNORECASE)


@dataclass
class Round:
    number: int          # 本批次内的编号，从 1 开始，写进抽取提示词
    round_idx: int       # 在会话里的序号
    at: datetime
    user_text: str
    assistant_text: str
    salience: float = 0.0

    def render(self) -> str:
        assistant = self.assistant_text
        lines = ["[Round %d]" % self.number]
        if self.user_text:
            lines.append("User: " + self.user_text)
        if assistant:
            lines.append("Assistant: " + assistant)
        return "\n".join(lines)


@dataclass
class NewFact:
    text: str
    kind: str
    subject: str
    attribute: str
    value: str
    event: Optional[DateSpan]
    rounds: List[int]
    importance: float
    confidence: float = 0.8      # 用户明确陈述 0.9，只是打算或拿不准 0.5，见 update.py
    source_quotes: List[Dict[str, Any]] = field(default_factory=list)
    semantics: Dict[str, Any] = field(default_factory=dict)
    relations: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class WriteReport:
    session_id: str
    rounds: int = 0
    skipped: int = 0
    added: List[int] = field(default_factory=list)
    duplicates: int = 0
    updates: List[Tuple[int, int]] = field(default_factory=list)   # 旧报告格式兼容；新增流程不再产生取代
    historical: int = 0          # 旧报告格式兼容
    tentative: int = 0           # 旧报告格式兼容
    uncertain: List[int] = field(default_factory=list)
    pending: List[int] = field(default_factory=list)
    extract_failures: int = 0
    extract_parts: int = 0             # 旧报告格式兼容；不再分块
    extract_split_rounds: int = 0       # 旧报告格式兼容；始终为零
    extract_truncated_rounds: int = 0   # 旧报告格式兼容；始终为零
    extract_calls: int = 0
    extract_input_tokens: int = 0
    extract_output_format: str = ""
    extract_response_bytes: int = 0
    extract_completion_tokens: int = 0
    extract_output_limit: int = 0
    extract_attempts: List[Dict[str, Any]] = field(default_factory=list)
    extract_model_facts: int = 0
    extract_quality: str = "not_checked"  # Protocol validity does not prove semantic completeness.
    extract_protocol_complete: bool = True
    extract_rejected_count: int = 0
    extract_rejections: List[Dict[str, Any]] = field(default_factory=list)
    extract_repaired_count: int = 0
    extract_repairs: List[Dict[str, Any]] = field(default_factory=list)
    extraction_diagnostic_id: str = ""
    quality_warnings: List[Dict[str, Any]] = field(default_factory=list)
    source_coverage: List[Dict[str, Any]] = field(default_factory=list)
    commit_retries: int = 0            # 复用抽取和向量结果的短事务重试次数
    extract_max_input_tokens: int = 0   # 整批对话的计数器单位；只记录，不限制输入长度
    structure_warnings: List[Dict[str, Any]] = field(default_factory=list)
    literal_structures: int = 0
    covered_literal_structures: int = 0
    relation_edges: int = 0
    relation_warnings: List[str] = field(default_factory=list)
    deferred: int = 0            # 没有记忆模型，留待以后补抽事实的轮次
    retry_rounds: List[int] = field(default_factory=list)  # 未完整抽取的轮次，持久排队后整轮重试
    resolve_fallbacks: int = 0   # 旧报告格式兼容；不再有裁决调用、修复或回退
    resolve_errors: List[str] = field(default_factory=list)
    resolve_repairs: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        line = "写入：%d 轮原文" % self.rounds
        if self.skipped:
            line += "（%d 轮寒暄未进索引）" % self.skipped
        line += " · 新增 %d 条记忆 · 重复 %d" % (len(self.added), self.duplicates)
        if self.commit_retries:
            line += " · 提交重试 %d 次" % self.commit_retries
        if self.extract_failures:
            line += " · %d 段抽取不完整，%d 轮已排队补抽" % (self.extract_failures, len(self.retry_rounds))
        if self.deferred:
            line += " · 没有记忆模型，%d 轮待以后补抽事实（memsys rebuild）" % self.deferred
        if self.extract_rejected_count:
            line += " · %d 条抽取已隔离，内容可能不完整" % self.extract_rejected_count
        if self.quality_warnings:
            line += " · %d 项内容覆盖提示待核对" % len(self.quality_warnings)
        return line


def group_turns(turns: Sequence[Mapping[str, Any]]) -> List[List[Mapping[str, Any]]]:
    """Group original messages into rounds while preserving their source positions."""
    groups = []
    for turn in turns:
        if not groups or (turn["role"] == "user" and any(t["role"] != "user" for t in groups[-1])):
            groups.append([])
        groups[-1].append(turn)
    return groups


def build_rounds(turns: Sequence[Mapping[str, Any]]) -> List[Tuple[datetime, str, str]]:
    """把逐句的日志拼成"轮"：一段用户发言加上紧随其后的助手回复。"""
    return [(from_iso(group[0]["at"]),
             "\n".join(t["content"] for t in group if t["role"] == "user").strip(),
             "\n".join(t["content"] for t in group if t["role"] != "user").strip())
            for group in group_turns(turns)]


def salience(user_text: str, assistant_text: str) -> float:
    """显著性门控的启发式打分：寒暄接近 0；含第一人称、数字、专名或较长的助手回答则加分。"""
    user = user_text.strip()
    if not user and not assistant_text.strip():
        return 0.0
    if _SMALL_TALK.match(user) and len(assistant_text) < 200:
        return 0.05
    score = 0.3
    if _FIRST_PERSON.search(user):
        score += 0.25
    if re.search(r"\d", user):
        score += 0.15
    if len(user.split()) >= 8 or len(user) >= 30:
        score += 0.1
    if re.search(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]+", user):
        score += 0.05
    if len(assistant_text) >= 300:
        score += 0.15
    return min(score, 1.0)


def _normalize_facts(data: Any, valid_numbers: Sequence[int]) -> List[NewFact]:
    items = data.get("facts") if isinstance(data, dict) else data
    out: List[NewFact] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        kind = str(item.get("kind") or "personal").strip().lower()
        raw_rounds = item.get("rounds")
        if raw_rounds is None:
            raw_rounds = []
        elif not isinstance(raw_rounds, list):
            raw_rounds = [raw_rounds]          # 模型偶尔写成 "rounds": 2
        rounds = []
        for value in raw_rounds:
            try:
                number = int(str(value).strip().lstrip("Rr"))
            except ValueError:
                continue
            if number in valid_numbers and number not in rounds:
                rounds.append(number)
        if not rounds and len(valid_numbers) == 1:
            rounds = list(valid_numbers)       # 只有一轮时出处没有歧义
        try:
            importance = min(max(int(item.get("importance") or 3), 1), 5)
        except (TypeError, ValueError):
            importance = 3
        from .memory_compaction import explicit_event_status
        semantics = explicit_event_status(dict(item, kind=kind), normalize_semantics(item.get('semantics')))
        out.append(NewFact(
            text=text,
            kind=kind if kind in FACT_KINDS else "personal",
            subject=str(item.get("subject") or "user").strip().lower() or "user",
            attribute=slug(str(item.get("attribute") or "")),
            value=validate_value(item.get("value")),
            event=parse_span(item.get("event_date")),
            rounds=rounds,
            importance=importance / 5.0,
            confidence=certainty_to_confidence(item.get("certainty")),
            source_quotes=[q for q in item.get("source_quotes", []) if isinstance(q, dict)]
                          if isinstance(item.get("source_quotes"), list) else [],
            semantics=semantics,
            relations=normalize_relations(
                item.get('relations'), data.get('entities') if isinstance(data, dict) else None)))
    return out


def salvage_objects(text: str) -> List[Any]:
    """从被截断的 {"facts": [ {...}, {...}, {... 里取出所有完整的对象。"""
    start = text.find("[")
    if start == -1:
        return []
    decoder, out, pos = json.JSONDecoder(), [], start + 1
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            return out
        try:
            obj, pos = decoder.raw_decode(text, pos)
        except ValueError:
            return out
        out.append(obj)


def _dedupe(facts: List[NewFact]) -> List[NewFact]:
    seen: Dict[str, NewFact] = {}
    retained = []
    for fact in facts:
        key = fact_identity(fact)
        from .memory_compaction import unanchored_event
        if unanchored_event(fact):
            key += '|source_rounds:' + json.dumps(sorted(fact.rounds))
        duplicate = seen.get(key)
        if duplicate is not None:
            duplicate.rounds = sorted(set(duplicate.rounds) | set(fact.rounds))
            duplicate.source_quotes.extend(fact.source_quotes)
            duplicate.relations = merge_relations(duplicate.relations, fact.relations)
        else:
            seen[key] = fact
            retained.append(fact)
    return retained


class Writer:
    def __init__(self, store: Store, models: Models, settings: MemorySettings,
                 tags: Optional[Mapping[str, str]] = None):
        self.store = store
        self.models = models
        self.settings = settings
        self.tags = dict(tags or {})
        self.tokens = budget_counter(settings)
        self.inserter = MemoryInserter(store)

    def consolidate(self, session_id: str, closed_at: Optional[datetime] = None,
                    upto_turn: Optional[int] = None) -> WriteReport:
        """整理会话里还没整理的轮次。closed_at 不为空时顺带关闭会话。"""
        self.tags["session_id"] = session_id
        report = WriteReport(session_id)
        fact_revision = self.store.get_meta("fact_revision", "0")
        session = self.store.session(session_id)
        start = session["turns_done"]
        rows = self.store.turns(session_id, start)
        if upto_turn is not None:
            rows = rows[:max(upto_turn - start, 0)]

        rounds: List[Round] = []
        base = self.store.next_round_idx(session_id)
        for i, (at, user_text, assistant_text) in enumerate(build_rounds(rows)):
            rounds.append(Round(i + 1, base + i, at, user_text, assistant_text,
                                salience(user_text, assistant_text)))
        active = [r for r in rounds if r.salience >= self.settings.gate_threshold]
        report.rounds, report.skipped = len(rounds), len(rounds) - len(active)

        session_at = rounds[0].at if rounds else from_iso(session["opened_at"])
        self.store.check_index_fingerprint(self.settings.index_fingerprint(self.models.embedding_name), legacy=True)
        raw_chunks = self.settings.recall_mode == "raw_chunks"
        # Raw ingestion is complete after indexing, even without a generation model.
        can_extract = raw_chunks or self.models.for_purpose("extract") is not None
        recent = [dict(role=t["role"], content=t["content"]) for t in self.store.conversation_turns(session_id)
                  if t["idx"] < start][-10:]
        facts = (_dedupe(self._extract(rounds, session_at, report, recent))
                 if rounds and can_extract and not raw_chunks else [])
        linked = {n for fact in facts for n in fact.rounds}
        active = [r for r in rounds if r.salience >= self.settings.gate_threshold or r.number in linked]
        if raw_chunks:
            active = [r for r in rounds if r.user_text.strip() or r.assistant_text.strip()]
            report.extract_quality = "not_applicable_raw_chunks"
        report.skipped = len(rounds) - len(active)
        if not can_extract:
            report.deferred = len(rounds)
        vectors, index = self._embed(facts, active)
        fact_vectors = vectors[:len(facts)]

        # New chat turns share the tenant publication head, but do not invalidate
        # extracted facts. Retry just publication when that head moves; rerun the
        # model only when facts or this session's consolidation cursor changed.
        for attempt in range(8):
            committed = copy.deepcopy(report)
            committed.commit_retries = attempt
            try:
                self._publish_rounds(session_id, start, len(rows), fact_revision, rounds, active,
                                     facts, vectors, fact_vectors, index, session_at, closed_at,
                                     can_extract, committed)
                return committed
            except WriteConflict:
                with self.store.read_snapshot():
                    current = self.store.session(session_id)
                    changed = (self.store.get_meta("fact_revision", "0") != fact_revision
                               or current["turns_done"] != start
                               or closed_at is not None and current["next_idx"] != start + len(rows))
                if changed or attempt == 7:
                    raise
                time.sleep(random.uniform(.005, .03) * (attempt + 1))

    def _publish_rounds(self, session_id, start, size, fact_revision, rounds, active,
                        facts, vectors, fact_vectors, index, session_at, closed_at,
                        can_extract, report):
        """Publish prepared results without calling extraction or embedding models."""
        linked = {number for fact in facts for number in fact.rounds}
        with self.store.transaction():
            if self.store.get_meta("fact_revision", "0") != fact_revision:
                raise WriteConflict("memories changed during extraction; retry with current context")
            episode_of: Dict[int, int] = {}
            for r in rounds:
                status = EPISODE_ACTIVE if r in active else EPISODE_SKIPPED
                score = max(r.salience, 0.6) if r.number in linked else r.salience
                episode_of[r.number] = self.store.add_episode(
                    session_id, r.round_idx, r.at, r.user_text, r.assistant_text, score, status)
            self.store.put_vectors("episode", [(episode_of[active[n].number], vectors[len(facts) + offset])
                                               for n, offset in index.representatives.items()])
            for n, chunks in index.chunks.items():
                self.store.put_episode_chunks(episode_of[active[n].number], stored_chunks(index, n, vectors, len(facts)))
            if index.texts or fact_vectors or not self.settings.index_originals:
                self.store.set_meta("index_fingerprint", self.settings.index_fingerprint(self.models.embedding_name))
                self.store.set_meta("index_manifest", json.dumps({"fingerprint": self.settings.index_fingerprint(self.models.embedding_name),
                    "strategy": "legacy", "index_originals": self.settings.index_originals,
                    "embedding_revision":self.settings.embedding_model_revision,
                    "counter": self.tokens.manifest(), "format": 2, **index_signature()}))
            new_vectors = []
            for fact, vec in zip(facts, fact_vectors):
                fact_id, created = self.inserter.add(fact, session_id, session_at,
                                      [episode_of[n] for n in fact.rounds], report)
                if created:
                    new_vectors.append((fact_id, vec))
                self._record_spans(fact, fact_id, episode_of)
                if self.settings.relations_enabled:
                    report.relation_edges += RelationIndex(self.store).publish(fact_id, fact.relations)
            self.store.put_vectors("fact", new_vectors)
            if not can_extract:
                self.store.add_backlog(episode_of[r.number] for r in rounds)
            else:
                self.store.add_backlog(episode_of[n] for n in report.retry_rounds)
            self.store.mark_consolidated(session_id, start + size, closed_at, expected_start=start)

    def backfill(self) -> List[WriteReport]:
        """给降级或抽取不完整的轮次补抽事实，去重后新增。

        每个会话一个事务：中途失败时已补完的会话保留，没补完的仍在 backlog 里，重跑接着补。
        """
        if self.settings.recall_mode == "raw_chunks":
            return []
        if self.models.for_purpose("extract") is None:
            raise ModelError("no memory model is available to extract facts; configure one first")
        groups: Dict[str, List[Episode]] = {}
        for episode in self.store.backlog():
            groups.setdefault(episode.session_id, []).append(episode)
        reports = []
        for session_id, episodes in sorted(groups.items(), key=lambda kv: (kv[1][0].at, kv[0])):
            self.tags["session_id"] = session_id
            report = WriteReport(session_id)
            fact_revision = self.store.get_meta("fact_revision", "0")
            rounds = [Round(i + 1, e.round_idx, e.at, e.user_text, e.assistant_text, e.salience)
                      for i, e in enumerate(episodes)]
            session_at = rounds[0].at
            facts = _dedupe(self._extract(rounds, session_at, report))
            vectors, _ = self._embed(facts, [])
            episode_of = {r.number: e.id for r, e in zip(rounds, episodes)}
            report.rounds = len(rounds)
            with self.store.transaction():
                if self.store.get_meta("fact_revision", "0") != fact_revision:
                    raise WriteConflict("memories changed during extraction; retry with current context")
                queued = {e.id for e in self.store.backlog()}
                if any(e.id not in queued for e in episodes):
                    raise WriteConflict("backfill sources changed or were already processed")
                new_vectors = []
                for fact, vec in zip(facts, vectors):
                    fact_id, created = self.inserter.add(fact, session_id, session_at,
                                          [episode_of[n] for n in fact.rounds], report)
                    if created:
                        new_vectors.append((fact_id, vec))
                    self._record_spans(fact, fact_id, episode_of)
                    if self.settings.relations_enabled:
                        report.relation_edges += RelationIndex(self.store).publish(fact_id, fact.relations)
                self.store.put_vectors("fact", new_vectors)
                failed = set(report.retry_rounds)
                self.store.clear_backlog(episode_of[r.number] for r in rounds if r.number not in failed)
            reports.append(report)
        return reports

    # -------------------------------------------------------------- extract
    def _record_spans(self, fact, fact_id, episode_of):
        if fact_id is None:
            return
        for quote in fact.source_quotes:
            try:
                number = int(str(quote.get("round", "")).lstrip("Rr"))
            except ValueError:
                continue
            if number in fact.rounds and number in episode_of:
                self.store.record_span(fact_id, episode_of[number], str(quote.get("role", "")).lower(),
                                       quote.get("quote", ""))

    def _extract(self, rounds: List[Round], session_at: datetime, report: WriteReport,
                 recent_messages: Sequence[Mapping[str, Any]] = ()) -> List[NewFact]:
        """整批消息完整抽取；输出触顶时仅增加预算，不切块或接续残缺 JSON。"""
        if not rounds:
            return []
        text = "\n\n".join(r.render() for r in rounds)
        context = [dict(id=fact.key, text=fact_text(fact), recorded_at=fact.recorded_at.isoformat(), relations=fact.relations)
                   for fact in self._existing_memories(text)]
        messages = [{"role": "system", "content": prompts.EXTRACT_SYSTEM},
                    {"role": "user", "content": prompts.EXTRACT_USER.format(
                        session_date=format_stamp(session_at), rounds=text,
                        existing_memories=json.dumps(context, ensure_ascii=False),
                        recent_messages=json.dumps(list(recent_messages), ensure_ascii=False))}]
        input_units = self.tokens.count(text)
        report.extract_max_input_tokens = max(report.extract_max_input_tokens, input_units)
        from .generation import complete_chat
        attempts, responses = [], []
        try:
            try:
                result = complete_chat(self.models.for_purpose('extract'),messages,purpose='extract',json_mode=True,
                    max_tokens=self.settings.extract_max_tokens,max_tokens_limit=self.settings.extract_max_tokens_limit,
                    tags=self.tags,attempts=attempts,responses=responses)
            finally:
                report.extract_calls += len(attempts)
                report.extract_input_tokens += input_units*len(attempts)
                report.extract_attempts.extend(attempts)
                report.extract_output_limit = attempts[-1]['max_tokens'] if attempts else self.settings.extract_max_tokens
                if responses:
                    report.extract_completion_tokens = responses[-1].completion_tokens
                    report.extract_response_bytes = len(responses[-1].text.encode('utf-8'))
            normalized, rejected, repaired = self._decode_extraction(result, rounds, context, recent_messages, report)
        except Exception as exc:
            report.extract_failures += 1
            report.extract_protocol_complete = False
            self._extraction_diagnostic(rounds, messages, responses, report, 'failed', error=str(exc))
            exc.extraction_report = asdict(report)
            exc.extraction_diagnostic_id = report.extraction_diagnostic_id
            raise
        if rejected or repaired or len(responses) > 1:
            self._extraction_diagnostic(rounds, messages, responses, report,
                'partial' if rejected else 'recovered', rejected=rejected, repaired=repaired)
        return normalized

    def _extraction_diagnostic(self, rounds, messages, responses, report, status, *, error='', rejected=(), repaired=()):
        """Keep full failed output outside fact/episode indexes, including on exceptions."""
        payload = json.dumps(dict(session=report.session_id, messages=messages,
            responses=[asdict(result) for result in responses]), ensure_ascii=False, sort_keys=True)
        key = hashlib.sha256(payload.encode()).hexdigest()
        report.extraction_diagnostic_id = key
        record = dict(id=key, session_id=report.session_id, status=status,
            rounds=[dict(number=r.number, round_idx=r.round_idx, at=r.at.isoformat()) for r in rounds],
            messages=messages, responses=[asdict(result) for result in responses],
            report=asdict(report), rejected=list(rejected), repaired=list(repaired), error=error)
        for attempt in range(8):
            try:
                with self.store.transaction():
                    old = self.store.record('extraction_diagnostic', key)
                    self.store.put_record('extraction_diagnostic', key, record)
                    if status in ('partial', 'failed') and not old:
                        health = self.store.record('extraction_health', 'current') or {}
                        self.store.put_record('extraction_health', 'current', dict(id='current',
                            unresolved_batches=health.get('unresolved_batches', 0) + 1,
                            rejected_entries=health.get('rejected_entries', 0) + len(rejected),
                            last_diagnostic_id=key, semantic_completeness_verified=False))
                return
            except WriteConflict:
                if attempt == 7:
                    raise
                time.sleep(random.uniform(.005, .03) * (attempt + 1))

    def _decode_extraction(self, result, rounds, context, recent_messages, report):
        if result.finish_reason in ("length", "incomplete"):
            raise IncompleteJSONError(result.finish_reason, output_limit=report.extract_output_limit,
                                      completion_tokens=result.completion_tokens)
        if result.finish_reason in ("content_filter", "refusal"):
            raise ModelError("memory extraction was not completed: " + result.finish_reason, status=422)
        data = parse_json(result.text)
        report.extract_output_format = 'objects'
        entries = data.get("facts") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ModelError("memory extraction must return a facts array", status=422)
        from .extraction_protocol import isolate_entries
        normalized, rejected, repaired = isolate_entries(entries, rounds, _normalize_facts,
            entities=data.get('entities') if isinstance(data, dict) else None)
        report.extract_protocol_complete = not rejected
        report.extract_rejected_count = len(rejected)
        report.extract_rejections = rejected[:16]
        report.extract_repaired_count = len(repaired)
        report.extract_repairs = repaired[:16]
        report.extract_model_facts = len(normalized)
        report.extract_quality = 'protocol_partial_not_semantically_verified' if rejected else 'not_semantically_verified'
        if rejected:
            report.quality_warnings.append(dict(code='quarantined_fact_entries', count=len(rejected),
                message='Invalid entries were isolated; retained facts remain usable, but content may be incomplete.'))
        if not normalized and any(any(text.strip() and not _SMALL_TALK.fullmatch(text.strip())
                for text in (r.user_text, r.assistant_text)) for r in rounds):
            report.quality_warnings.append(dict(code='empty_model_extraction',
                rounds=[r.number for r in rounds],
                message='Model returned no facts for non-greeting content; semantic coverage is unverified. '
                        'Literal supplements do not prove completeness. No automatic re-extraction was made.'))
        from .write_coverage import source_coverage
        report.source_coverage, warnings = source_coverage(data, rounds, normalized,
            is_trivial=lambda text:bool(_SMALL_TALK.fullmatch(text)))
        report.quality_warnings.extend(warnings)
        if self.settings.relations_enabled:
            declared = sum(len(item.get('relations', [])) for item in entries
                           if isinstance(item, dict) and isinstance(item.get('relations'), list))
            if declared > sum(len(f.relations) for f in normalized):
                report.relation_warnings.append('Invalid relation protocol entries were excluded; facts were retained.')
            ground_relations(normalized, rounds, reference_text=json.dumps(context, ensure_ascii=False) + '\n' +
                             json.dumps(list(recent_messages), ensure_ascii=False), report=report)
        else:
            for fact in normalized:
                fact.relations = []
        literals = self._literal_facts(rounds)
        report.literal_structures = len(literals)
        from .memory_compaction import omit_covered_literals
        retained, report.covered_literal_structures = omit_covered_literals(normalized, literals)
        normalized.extend(retained)
        for r in rounds:
            source = r.user_text + '\n' + r.assistant_text
            table = bool(re.search(r'(?m)^\s*\|.*\|\s*$', source))
            numbered = bool(re.search(r'(?m)^\s*\d+[.)]\s+\S', source))
            related = [f for f in normalized if r.number in f.rounds]
            structured = any(f.value.startswith(('{', '[')) for f in related)
            if (table or numbered) and not structured:
                report.structure_warnings.append(dict(round=r.number, table=table, numbered_list=numbered,
                    message='Source contains structured content but no structured value was retained; semantic coverage is unverified.'))
        return normalized, rejected, repaired

    @staticmethod
    def _literal_facts(rounds):
        facts=[]
        for r in rounds:
            topic=[t for t in terms(r.user_text) if any(c.isalpha() for c in t)][:8]
            for structure in assistant_structures(r.assistant_text):
                value=validate_value(structure.value)
                facts.append(NewFact(text='The assistant supplied an ordered %s.' % structure.kind,
                    kind='assistant',subject='assistant',attribute='provided_'+structure.kind,
                    value=value,event=None,rounds=[r.number],importance=.6,confidence=.9,
                    source_quotes=[dict(round=r.number,role='assistant',quote=quote)
                                   for quote in [structure.quote, *structure.context_quotes]],
                    semantics=normalize_semantics(dict(value_type='other',object_key='assistant_'+structure.kind,
                        event_key='structure@'+r.at.isoformat()+'#'+str(r.round_idx),
                        qualifiers=['conversation step: '+str(r.round_idx+1)]+(['topic: '+', '.join(topic)] if topic else [])))))
        return facts

    def restore_structures(self):
        """Append literal structures from safe saved episodes without re-extraction."""
        from .schema import SEARCHABLE_WITH_ARCHIVE
        # The initial read only discovers session IDs. Prepare each session from
        # one fresh snapshot, so a source hidden between discovery and preparation
        # cannot be restored using stale episode objects and a newer fingerprint.
        session_ids=sorted({e.session_id for e in self.store.episodes(statuses=SEARCHABLE_WITH_ARCHIVE['episode'])})
        reports=[]
        for sid in session_ids:
            with self.store.read_snapshot():
                episodes=[e for e in self.store.episodes(statuses=SEARCHABLE_WITH_ARCHIVE['episode']) if e.session_id==sid]
                episodes.sort(key=lambda e:(e.round_idx,e.id))
                revision=self.store.get_meta('fact_revision','0')
                source=self.store.index_source_fingerprint()
                rounds=[Round(i+1,e.round_idx,e.at,e.user_text,e.assistant_text,e.salience) for i,e in enumerate(episodes)]
                facts=_dedupe(self._literal_facts(rounds))
            if not facts:continue
            vectors,_=self._embed(facts,[])
            report=WriteReport(sid,rounds=len(rounds),literal_structures=len(facts))
            episode_of={r.number:e.id for r,e in zip(rounds,episodes)}
            with self.store.transaction():
                if self.store.get_meta('fact_revision','0')!=revision or self.store.index_source_fingerprint()!=source:
                    raise WriteConflict('saved structure sources changed; retry restoration')
                new=[]
                for fact,vector in zip(facts,vectors):
                    fid,created=self.inserter.add(fact,sid,episodes[0].at,[episode_of[n] for n in fact.rounds],report)
                    if created:new.append((fid,vector))
                    self._record_spans(fact,fid,episode_of)
                self.store.put_vectors('fact',new)
            reports.append(report)
        return reports

    def _existing_memories(self, text: str) -> List[Any]:
        """同一次抽取所需的相关记忆；只有向量检索，不调用生成式模型。"""
        limit = self.settings.extract_context_memories
        if limit <= 0 or not self.store.get_meta("embedding_model"):
            return []
        self.store.check_embedding_model(self.models.embedding_name)
        vectors = self.models.embedder.embed([text], purpose="embed", tags=self.tags)
        if len(vectors) != 1:
            raise ModelError("extraction context query requires one embedding")
        hits = self.store.vector_search("fact", normalize(vectors[0]), (FACT_CURRENT,), limit)
        by_id = {f.id: f for f in self.store.facts([fid for fid, _ in hits])}
        return [by_id[fid] for fid, _ in hits if fid in by_id]

    def _embed(self, facts: List[NewFact], rounds: List[Round]) -> Tuple[List[List[float]], EpisodeIndex]:
        self.store.check_index_fingerprint(self.settings.index_fingerprint(self.models.embedding_name), legacy=True)
        if self.settings.recall_mode == "raw_chunks":
            from .raw_chunks import raw_episode_index
            index = raw_episode_index([(r.user_text, r.assistant_text) for r in rounds], self.settings)
        else:
            index = episode_index([(r.user_text, r.assistant_text) for r in rounds], self.settings)
        texts = [fact_text(f) for f in facts] + index.texts
        if not texts:
            return [], index
        self.store.check_embedding_model(self.models.embedding_name)
        vectors = [normalize(v) for v in self.models.embedder.embed(texts, purpose="embed", tags=self.tags)]
        if len(vectors) != len(texts):
            # 数量对不上时后面按位置配对会悄悄丢掉事实，宁可整次失败、下次重试
            raise ModelError("sent %d texts, got %d embeddings back" % (len(texts), len(vectors)))
        return vectors, index
