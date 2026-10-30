"""回放跑分：逐题把历史会话按时间喂给记忆核心，再在提问时间提问，输出官方要求的 JSONL。

- 每题一个独立的记忆库（<输出目录>/stores/<题号>.sqlite），题与题互不影响。
- 写入结果会缓存：只改读取策略时不必重跑抽取；写入相关的配置或记忆模型变了会自动重建；
  只换了 embedding 时只重算向量（reindex）。注意更新裁决当时是按旧向量找的候选，要严格复现用 --reingest。
- 可以续跑：已经有答案的题跳过；单题出错记进 errors.jsonl，不影响其他题。
  续跑时配置（模型、参数、提示词、数据文件）和上次不一样会拒绝，免得一份答案文件里混着两套配置。
- mode=memory 是正式方案。full-context（全量拼接）和 no-memory 只是对照组，不能拿去提交。
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .. import prompts
from ..config import MemorySettings, Models
from ..core import MemorySystem, _safe_name
from ..llm import ModelError
from ..read import parse_answer
from ..read_budget import is_read_service_error
from ..textutil import estimate_tokens
from ..timeutil import format_day, format_stamp
from .jsonl import append_jsonl, read_jsonl
from .longmemeval import ReplayItem
from .provenance import (answer_model_settings, fingerprint as config_fingerprint, model_manifest,
                         prompt_fingerprint, source_manifest)

MODES = ("memory", "full-context", "no-memory")


class ConfigChanged(ValueError):
    """续跑时的配置和已有答案的配置不一致。"""


def _done_ids(path: str) -> Set[str]:
    return {str(r["question_id"]) for r in read_jsonl(path) if "question_id" in r and "hypothesis" in r}


def config_diff(previous: Mapping[str, Any], current: Mapping[str, Any]) -> List[str]:
    """两份 run_config 里会影响答案的差别。旧配置里没有的记忆参数按当前默认值算（新增参数不算变化）。"""
    diffs = []
    def comparable(config: Mapping[str, Any], key: str) -> Any:
        value = config.get(key)
        if key == "models" and isinstance(value, dict):   # 换评分模型不影响答案
            value = {role: name for role, name in value.items() if role != "judge"}
        return value

    for key in ("mode", "models", "embedding", "write_fingerprint", "data", "execution_limits"):
        if key in previous and comparable(previous, key) != comparable(current, key):
            diffs.append("%s: %s → %s" % (key, json.dumps(comparable(previous, key), ensure_ascii=False),
                                          json.dumps(comparable(current, key), ensure_ascii=False)))
    for key in ("model_settings", "prompt_fingerprint", "provenance"):
        if key not in previous:
            if key in current:
                diffs.append("%s: previous run lacks reproducibility metadata" % key)
            continue
        old, new = previous[key], current.get(key)
        if key == "model_settings":
            old, new = answer_model_settings(old), answer_model_settings(new or {})
        if key == "provenance":
            old, new = old.get("source_sha256"), (new or {}).get("source_sha256")
        if old != new:
            diffs.append("%s: source or settings changed" % key)
    defaults = MemorySettings().to_dict()
    old_memory, new_memory = previous.get("memory", {}), current.get("memory", {})
    for key in sorted(set(old_memory) | set(new_memory)):
        if key == "confirm_conflicts":
            continue
        old, new = old_memory.get(key, defaults.get(key)), new_memory.get(key, defaults.get(key))
        if old != new:
            diffs.append("memory.%s: %r → %r" % (key, old, new))
    return diffs


class Runner:
    def __init__(self, models: Models, settings: MemorySettings, out_dir: str, mode: str = "memory",
                 reingest: bool = False, progress: Optional[Callable[[str], None]] = None,
                 data_info: Optional[Mapping[str, Any]] = None, allow_config_change: bool = False,
                 max_wall_s: float = 3600.0, stop_after_service_errors: int = 2):
        import math
        if not math.isfinite(max_wall_s) or max_wall_s <= 0 or stop_after_service_errors < 1:
            raise ValueError('run wall budget must be positive; service error limit must be at least one')
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.models = models
        # 评测回放没有用户可问，显式确认必须关掉，冲突一律按时间优先、两条都留
        self.settings = dataclasses.replace(settings, confirm_conflicts=False)
        self.out_dir = out_dir
        self.mode = mode
        self.reingest = reingest
        self.progress = progress or (lambda message: None)
        self.data_info = dict(data_info or {})
        self.allow_config_change = allow_config_change
        self.paths = {name: os.path.join(out_dir, name) for name in
                      ("hypotheses.jsonl", "timings.jsonl", "errors.jsonl", "run_config.json")}
        self.stores_dir = os.path.join(out_dir, "stores")
        self.traces_dir = os.path.join(out_dir, "traces")
        self._lock = threading.Lock()
        self.aborted: Optional[str] = None   # 遇到账户问题（key 无效、余额不足）时整批停下
        self.max_wall_s = max_wall_s
        self.stop_after_service_errors = stop_after_service_errors
        self.cancel_event = threading.Event()
        self._consecutive_service_errors = 0
        self._last_service_error = None
        self.provenance = source_manifest()
        self.model_settings = model_manifest(models)
        os.makedirs(self.traces_dir, exist_ok=True)

    def run(self, items: Sequence[ReplayItem], workers: int = 1) -> Dict[str, int]:
        done = _done_ids(self.paths["hypotheses.jsonl"])
        todo = [item for item in items if item.question_id not in done]
        self._write_config(len(items), resuming=bool(done))
        counts = {"answered": 0, "errors": 0, "skipped": len(items) - len(todo), "not_started": 0}
        started = time.monotonic()
        self.deadline = started + self.max_wall_s
        def expire():
            with self._lock:
                self.aborted = self.aborted or 'batch wall-clock budget exhausted'
                self.cancel_event.set()
        timer = threading.Timer(self.max_wall_s, expire)
        timer.daemon = True
        timer.start()

        def one(item: ReplayItem) -> str:
            if self.aborted or self.cancel_event.is_set():
                with self._lock:
                    counts["not_started"] += 1
                return "not_started"
            outcome = self._one(item)
            with self._lock:
                counts["answered" if outcome == "ok" else "errors"] += 1
                finished = counts["answered"] + counts["errors"]
                self.progress("[%d/%d] %s %s (%.0fs elapsed)" % (
                    finished, len(todo), item.question_id, outcome, time.monotonic() - started))
            return outcome

        try:
            if workers <= 1:
                for item in todo:
                    one(item)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    try:
                        list(pool.map(one, todo))
                    except KeyboardInterrupt:
                        self.cancel_event.set()
                        raise
        except KeyboardInterrupt:
            self.cancel_event.set()
            self.aborted = 'interrupted by user'
            raise
        finally:
            timer.cancel()
            completed = len(_done_ids(self.paths['hypotheses.jsonl']))
            with open(os.path.join(self.out_dir, 'execution.json'), 'w', encoding='utf-8') as stream:
                json.dump(dict(status='completed' if completed == len(items) else 'incomplete',
                               completed=completed, expected=len(items), counts=counts, reason=self.aborted,
                               wall_s=round(time.monotonic()-started, 3), max_wall_s=self.max_wall_s,
                               stop_after_service_errors=self.stop_after_service_errors), stream, indent=2)
        return counts

    def _one(self, item: ReplayItem) -> str:
        started = time.monotonic()
        try:
            if self.mode == "memory":
                answer, timing = self._memory(item)
            else:
                answer, timing = self._baseline(item)
        except Exception as exc:  # 单题失败不拖垮整次评测，记下来之后用续跑补上
            with self._lock:
                if isinstance(exc, ModelError) and exc.account_problem and not self.aborted:
                    self.aborted = str(exc)[:300]
                    self.cancel_event.set()
                if is_read_service_error(exc):
                    kind = (getattr(exc, 'read_state', 'service_error'), getattr(exc, 'status', None))
                    self._consecutive_service_errors = (self._consecutive_service_errors + 1
                                                        if kind == self._last_service_error else 1)
                    self._last_service_error = kind
                    if self._consecutive_service_errors >= self.stop_after_service_errors:
                        self.aborted = self.aborted or 'consecutive read service failures'
                        self.cancel_event.set()
                else:
                    self._consecutive_service_errors = 0
                    self._last_service_error = None
            self._append("errors.jsonl", {"question_id": item.question_id, "error": repr(exc),
                                          "read_state": getattr(exc, 'read_state', 'error'),
                                          "elapsed_s": round(time.monotonic() - started, 3),
                                          "read_transport": getattr(exc, 'read_transport', None),
                                          "traceback": traceback.format_exc()})
            return "error"
        with self._lock:
            self._consecutive_service_errors = 0
            self._last_service_error = None
        self._append("hypotheses.jsonl", {"question_id": item.question_id, "hypothesis": answer})
        self._append("timings.jsonl", dict(timing, question_id=item.question_id, mode=self.mode))
        return "ok"

    def _memory(self, item: ReplayItem, *, ingest_only: bool = False) -> Tuple[Optional[str], Dict[str, object]]:
        tags = {"question_id": item.question_id}
        write_models = {role: details for role, details in answer_model_settings(self.model_settings).items()
                        if role in ("extract", "resolve")}
        write_part = "%s|%s" % (self.settings.write_fingerprint(), config_fingerprint(
            [write_models, self.models.embedding_name, answer_model_settings(self.model_settings).get("embedding"),
             self.provenance["write_source_sha256"],
             [(session.session_id, session.date.isoformat(), [(t.role, t.content) for t in session.turns])
              for session in item.sessions]]))
        fingerprint = "%s|%s" % (write_part, self.models.embedding_name)
        store_path = os.path.join(self.stores_dir, _safe_name(item.question_id) + ".sqlite")
        system = MemorySystem(self.models, self.settings, data_dir=self.stores_dir, trace_dir=self.traces_dir)
        reindexed = False
        try:
            store = system.store(item.question_id)
            previous = store.get_meta("ingest_fingerprint") or ""
            ingested = not self.reingest and store.get_meta("ingested") == "1"
            t0 = time.monotonic()
            cached = ingested and previous == fingerprint
            if cached and store.get_meta("index_fingerprint") != self.settings.index_fingerprint(self.models.embedding_name):
                # 同一事实库仅变分块/索引时重建检索层。换事实向量空间则另做完整写入。
                system.reindex(item.question_id)
                reindexed = True
            if not cached:
                coverage = {key: 0 for key in ("rounds", "skipped", "extract_parts", "extract_split_rounds",
                                              "extract_truncated_rounds", "extract_calls", "extract_failures",
                                              "extract_input_tokens", "deferred", "extract_max_input_tokens", "resolve_fallbacks")}
                retry_rounds = 0
                resolve_errors = []
                resolve_repairs = []
                if store.counts()["session"]:
                    # 旧的、写入配置不同的或上次写到一半的记忆库：整个重建，不在上面追加
                    system.close()
                    for path in (store_path, store_path + "-journal"):
                        if os.path.exists(path):
                            os.remove(path)
                    store = system.store(item.question_id)
                for session in item.sessions:
                    handle = system.open_session(item.question_id, session.date, session.session_id)
                    for turn in session.turns:
                        role = "user" if turn.role == "user" else "assistant"
                        system.add_turn(handle, role, turn.content, session.date)
                    write_report, _ = system.close_session(handle, session.date, tags=tags)
                    retry_rounds += len(write_report.retry_rounds)
                    resolve_errors.extend(write_report.resolve_errors)
                    resolve_repairs.extend(write_report.resolve_repairs)
                    for key in coverage:
                        value = getattr(write_report, key)
                        coverage[key] = (max(coverage[key], value) if key == "extract_max_input_tokens"
                                         else coverage[key] + value)
                coverage["retry_rounds"] = retry_rounds
                coverage["pending_extract_rounds"] = store.backlog_count()
                coverage["resolve_errors"] = resolve_errors
                coverage["resolve_repairs"] = resolve_repairs
                store.set_meta("ingest_write_coverage", json.dumps(coverage))
                store.set_meta("ingest_fingerprint", fingerprint)
                store.set_meta("ingested", "1")
            t1 = time.monotonic()
            coverage = json.loads(store.get_meta("ingest_write_coverage") or "null")
            if ingest_only:
                # A separate write stage prepares exactly the normal cache,
                # without generating or recording a placeholder answer.
                return None, {"ingest_s": round(t1 - t0, 3), "cached_ingest": cached,
                              "reindexed": reindexed, "write_coverage": coverage,
                              "memory_size": store.counts()}
            reply = system.respond(item.question_id, item.question, item.question_date, tags=tags,
                                   cancel_event=self.cancel_event,
                                   read_timeout_s=max(0.001, self.deadline - time.monotonic()))
            t2 = time.monotonic()
        finally:
            system.close()
        stages = reply.trace["timings"]
        return reply.text, {"ingest_s": round(t1 - t0, 3), "answer_s": round(t2 - t1, 3),
                            "plan_s": stages["plan_s"], "search_s": stages["search_s"],
                            "rerank_s": stages["rerank_s"], "generate_s": stages["answer_s"],
                            "cached_ingest": cached, "reindexed": reindexed,
                            "write_coverage": coverage,
                            "index_coverage": reply.trace["index_coverage"],
                            "aggregation": reply.trace["aggregation"],
                            "read_transport": reply.trace["read_transport"],
                            "read_state": reply.trace["read_state"],
                            "reranking": {key:value for key,value in reply.trace["reranking"].items() if key not in ("windows","scores")},
                            "context_tokens": reply.trace["context_tokens"],
                            "memory_tokens": reply.trace["memory_tokens"],
                            "effective_budget": reply.trace["effective_budget"],
                            "prompt_tokens_estimate": reply.trace["prompt_tokens_estimate"],
                            "history_tokens": reply.trace["history_tokens"],
                            "memory_facts": reply.trace["memory_size"]["fact"],
                            "memory_episodes": reply.trace["memory_size"]["episode"]}

    def _baseline(self, item: ReplayItem) -> Tuple[str, Dict[str, object]]:
        """对照组。full-context：整段历史直接塞进上下文；no-memory：完全不给历史。"""
        tags = {"question_id": item.question_id}
        history = "\n\n".join(
            "### Session on %s\n" % format_stamp(s.date)
            + "\n".join("%s: %s" % ("User" if t.role == "user" else "Assistant", t.content) for t in s.turns)
            for s in item.sessions)
        if self.mode == "full-context":
            prompt = prompts.BASELINE_QA.format(today=format_day(item.question_date), history=history,
                                                question=item.question, rules=prompts.ANSWER_RULES)
        else:
            prompt = prompts.ANSWER_QA.format(today=format_day(item.question_date),
                                              memory="## Memory\n(memory is disabled in this baseline)",
                                              question=item.question, rules=prompts.ANSWER_RULES)
        t0 = time.monotonic()
        from ..read_budget import ReadBudget, read_scope
        from ..read_retry import ReadTransportChat
        s = self.settings
        budget = ReadBudget(min(s.read_timeout_s, max(0.001, self.deadline - t0)),
                            s.read_retries_per_call, s.read_retries_total, s.read_retry_wait_s, self.cancel_event)
        with read_scope(budget):
            try:
                output = ReadTransportChat(self.models.chat, budget).chat(
                    [{"role": "system", "content": prompts.ANSWER_SYSTEM}, {"role": "user", "content": prompt}],
                    purpose="answer_" + self.mode.replace("-", "_"), max_tokens=self.settings.answer_max_tokens,
                    tags=tags).text
            except ModelError as exc:
                exc.read_transport = budget.snapshot()
                raise
        notes, answer = parse_answer(output)
        with open(os.path.join(self.traces_dir, _safe_name(item.question_id) + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"mode": self.mode, "question": item.question, "notes": notes, "answer": answer},
                      fh, ensure_ascii=False, indent=1)
        return answer, {"ingest_s": 0.0, "answer_s": round(time.monotonic() - t0, 3), "cached_ingest": False,
                        "context_tokens": estimate_tokens(prompt), "history_tokens": estimate_tokens(history)}

    def _append(self, name: str, record: Dict[str, object]) -> None:
        with self._lock:
            append_jsonl(self.paths[name], record)

    def _write_config(self, total: int, resuming: bool) -> None:
        """写 run_config.json。续跑时先和上次的配置比对：不一致就拒绝，除非明确允许（允许时把旧配置留在 history 里）。"""
        config: Dict[str, Any] = {
            "mode": self.mode, "questions": total, "models": self.models.names,
            "model_sources": self.models.sources, "tier": self.models.tier,
            "embedding": self.models.embedding_name, "memory": self.settings.to_dict(),
            "execution_limits": dict(max_wall_s=self.max_wall_s,
                                     stop_after_service_errors=self.stop_after_service_errors),
            "write_fingerprint": self.settings.write_fingerprint(),
            "index_fingerprint": self.settings.index_fingerprint(self.models.embedding_name),
            "read_fingerprint": self.settings.read_fingerprint(),
            "model_settings": self.model_settings, "provenance": self.provenance,
            "prompt_fingerprint": prompt_fingerprint(),
            "started_at": datetime.now().replace(microsecond=0).isoformat()}
        if self.data_info:
            config["data"] = self.data_info
        path = self.paths["run_config.json"]
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                previous = json.load(fh)
            history = previous.pop("history", [])
            diffs = config_diff(previous, config) if resuming else []
            if diffs and not self.allow_config_change:
                raise ConfigChanged("这个输出目录里已有用另一套配置作答的答案，续跑会把两套配置的结果混在一起：\n  "
                                    + "\n  ".join(diffs)
                                    + "\n换一个 --out 目录重新跑；确实要接着跑就加 --allow-config-change。")
            if diffs:
                config["history"] = history + [dict(previous, changed=diffs)]
            elif history:
                config["history"] = history
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=1)
