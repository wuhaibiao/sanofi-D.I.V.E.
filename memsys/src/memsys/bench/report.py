"""评估报告：把一次跑分的答案、评分结果、耗时、token 和费用汇总成 Markdown。

覆盖手册 C.3 要求披露的内容：Overall Accuracy、分题型准确率、拒答题准确率、Task-averaged Accuracy、
模型与参数、全量题目的 token / 费用 / 耗时；另外给出"上下文 / 历史"token 比，证明不是全量拼接。
"""

from __future__ import annotations

import glob
import json
import os
from typing import Dict, List, Optional, Sequence

from ..llm import UsageLedger
from .jsonl import read_jsonl
from .longmemeval import GoldLabel

TYPE_ORDER = ("single-session-user", "single-session-assistant", "single-session-preference",
              "multi-session", "knowledge-update", "temporal-reasoning")


_read_jsonl = read_jsonl

# 报告里列出的关键参数；完整参数在 run_config.json
KEY_PARAMS = ("context_budget_tokens", "fact_budget_share", "max_facts", "max_episodes", "query_analysis",
              "llm_rerank", "min_similarity", "resolve_similarity", "gate_threshold", "extract_chunk_tokens",
              "extract_full_rounds", "query_focused_excerpts", "episode_chunk_vectors", "embed_chunk_tokens",
              "embed_chars", "aggregation_validation", "rerank_backend", "chunk_strategy",
              "query_policy", "answer_review_policy")

# 按记忆规模分组看检索耗时
SIZE_BINS = ((0, 50), (51, 200), (201, 1000), (1001, None))


def _first_per_question(records: List[dict]) -> List[dict]:
    """同一题有多条记录时取第一条，与续跑、评分的口径一致。"""
    seen, out = set(), []
    for record in records:
        qid = record.get("question_id")
        if qid is not None and qid not in seen:
            seen.add(qid)
            out.append(record)
    return out


def _stats_row(name: str, values: Sequence[float], scale: float = 1.0, unit: str = "s", digits: int = 2) -> str:
    if not values:
        return "| %s | 0 | — | — | — |" % name
    fmt = "%%.%df %s" % (digits, unit)
    return "| %s | %d | %s | %s | %s |" % (
        name, len(values), fmt % (scale * sum(values) / len(values)), fmt % (scale * _quantile(values, .5)),
        fmt % (scale * _quantile(values, .95)))


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def _pct(hits: int, total: int) -> str:
    return "%.1f%% (%d/%d)" % (100.0 * hits / total, hits, total) if total else "—"


def find_eval_log(run_dir: str) -> Optional[str]:
    """有多个评分文件时取评过的题最多的；一样多时优先模型评分，其次按文件名。"""
    logs = sorted(glob.glob(os.path.join(run_dir, "hypotheses.jsonl.eval-results-*")))
    if not logs:
        return None
    def rank(path: str):
        judged = len({r.get("question_id") for r in read_jsonl(path) if "autoeval_label" in r})
        return (-judged, path.endswith("-offline-rules"), path)
    return min(logs, key=rank)


def _known_cost(record) -> bool:
    return record.cost_known is True or (record.cost_known is None and record.cost > 0)


def _cost_cell(records) -> str:
    currencies = {r.pricing.get("currency") for r in records if _known_cost(r) and r.cost}
    if records and all(_known_cost(r) for r in records) and len(currencies) <= 1:
        return "%.4f" % sum(r.cost for r in records)
    return "未完整估算" if any(_known_cost(r) for r in records) else "未知"


def build_report(run_dir: str, golds: Dict[str, GoldLabel], eval_log: Optional[str] = None) -> str:
    config = {}
    if os.path.exists(os.path.join(run_dir, "run_config.json")):
        with open(os.path.join(run_dir, "run_config.json"), encoding="utf-8") as fh:
            config = json.load(fh)
    hypotheses = _first_per_question([r for r in _read_jsonl(os.path.join(run_dir, "hypotheses.jsonl"))
                                      if "hypothesis" in r])
    dataset_ids = set(golds)
    foreign_answers = {h["question_id"] for h in hypotheses} - dataset_ids
    hypotheses = [h for h in hypotheses if h["question_id"] in dataset_ids]
    answered = {h["question_id"] for h in hypotheses}
    # 只统计仍在答案文件里的题；重答过的题以最后一次的耗时为准
    timings = list({t["question_id"]: t for t in _read_jsonl(os.path.join(run_dir, "timings.jsonl"))
                    if t.get("question_id") in answered}.values())
    errors = _read_jsonl(os.path.join(run_dir, "errors.jsonl"))
    eval_log = eval_log or find_eval_log(run_dir)
    labels = {r["question_id"]: bool(r["autoeval_label"]["label"])
              for r in _first_per_question([r for r in _read_jsonl(eval_log) if "autoeval_label" in r])} \
        if eval_log else {}
    labels = {qid: label for qid, label in labels.items() if qid in answered}
    failed = ({e.get("question_id") for e in errors} & dataset_ids) - answered
    missing = dataset_ids - answered
    not_started = missing - failed
    unjudged = answered - set(labels)
    provenance = config.get("provenance", {})

    lines = ["# 评估报告：%s" % os.path.basename(os.path.normpath(run_dir)), ""]
    lines += ["## 配置", "",
              "- 模式：%s%s" % (config.get("mode", "?"),
                             "" if config.get("mode") in (None, "memory") else "（对照组，不能用于提交）"),
              "- 模型：%s" % json.dumps(config.get("models", {}), ensure_ascii=False),
              "- embedding：%s" % config.get("embedding", "?"),
              "- 写入配置指纹：%s" % config.get("write_fingerprint", "?"),
              "- 数据：%s" % ("%s（sha256 前 16 位 %s）" % (config["data"].get("file"), config["data"].get("sha256"))
                            if config.get("data") else "未记录（旧版本的输出目录）"),
              "- 关键参数：%s" % (", ".join("%s=%s" % (k, config["memory"][k]) for k in KEY_PARAMS
                                       if k in config.get("memory", {})) or "?"),
              "- 完整参数见 `run_config.json`",
              "- 代码版本：%s%s；源码 sha256：%s" % (provenance.get("git_commit", "未记录"),
                                                    "（含未提交源码修改）" if provenance.get("uncommitted_source") else "",
                                                    provenance.get("source_sha256", "未记录")),
              "- 提示词 sha256：%s" % config.get("prompt_fingerprint", "未记录"),
              "- 答案覆盖：%d / %d 题；缺 %d 题（失败 %d，未开始 %d）" % (
                  len(answered), len(golds), len(missing), len(failed), len(not_started)),
              "- 已评分：%d / %d 道已作答题；未评分 %d 题" % (len(labels), len(answered), len(unjudged)),
              ""]
    if foreign_answers:
        lines += ["**注意**：答案文件含 %d 个不属于当前数据集的题号，已从统计中排除。" % len(foreign_answers), ""]
    if config.get("history"):
        lines += ["**注意**：这个输出目录续跑时改过配置（用 --allow-config-change 允许），答案来自不同配置：", ""]
        for old in config["history"]:
            lines.append("- 到 %s 为止：%s" % (old.get("started_at", "?"), "; ".join(old.get("changed", []))))
        lines.append("")

    lines += ["## 准确率", ""]
    if not labels:
        lines += ["还没有评分结果。先运行官方 evaluate_qa.py 或 `memsys bench judge`，再重新生成报告。", ""]
    else:
        lines += ["评分文件：`%s`（评分模型：%s）" % (os.path.basename(eval_log), _judge_model(eval_log)), ""]
        if _judge_model(eval_log) == "offline-rules":
            lines += ["**注意：这是离线规则的近似评分，不是官方口径**（官方用 GPT-4o 判断）。偏好题无法用规则判断，没有计入。", ""]
        if unjudged:
            lines += ["已作答但未评分：%d 题。" % len(unjudged), ""]
        judged = [q for q in labels if q in golds]
        hits = sum(labels[q] for q in judged)
        lines += ["| 题型 | 准确率 |", "|---|---|", "| **Overall** | **%s** |" % _pct(hits, len(judged))]
        per_type = {}
        for qtype in TYPE_ORDER + tuple(sorted({golds[q].question_type for q in judged} - set(TYPE_ORDER))):
            ids = [q for q in judged if golds[q].question_type == qtype]
            if ids:
                per_type[qtype] = sum(labels[q] for q in ids) / len(ids)
                lines.append("| %s | %s |" % (qtype, _pct(sum(labels[q] for q in ids), len(ids))))
        abstention = [q for q in judged if golds[q].is_abstention]
        lines.append("| 拒答题（_abs） | %s |" % _pct(sum(labels[q] for q in abstention), len(abstention)))
        if per_type:
            lines.append("| Task-averaged | %.1f%% |" % (100.0 * sum(per_type.values()) / len(per_type)))
        lines += ["", "以上为当前实验的已评分题准确率；正式 Benchmark 成绩以项目组对全量 500 题统一复跑为准。", ""]

    coverage = [t["write_coverage"] for t in timings if isinstance(t.get("write_coverage"), dict)]
    if coverage:
        lines += ["## 写入覆盖", "",
                  "以下描述已作答题的源记忆库，包含复用缓存的原始写入；不能作为本次实际调用或费用统计。", "",
                  "- 有覆盖记录：%d / %d 道已作答题。" % (len(coverage), len(answered)),
                  "- 轮次：%d；超长轮次分段：%d；旧前缀模式截断：%d。" % (
                      sum(c.get("rounds", 0) for c in coverage), sum(c.get("extract_split_rounds", 0) for c in coverage),
                      sum(c.get("extract_truncated_rounds", 0) for c in coverage)),
                  "- 无模型待补抽轮次：%d；抽取不完整轮次：%d；输出失败段：%d；写入结束仍待补抽：%d。" % (
                      sum(c.get("deferred", 0) for c in coverage), sum(c.get("retry_rounds", 0) for c in coverage),
                      sum(c.get("extract_failures", 0) for c in coverage),
                      sum(c.get("pending_extract_rounds", c.get("deferred", 0)) for c in coverage)),
                  "- 单次抽取的对话部分最大估算 token：%d（含分段标签，不含系统提示词与抽取规则）。" % (
                      max(c.get("extract_max_input_tokens", 0) for c in coverage)),
                  "", "完整输入覆盖不保证模型完整抽取，回答质量仍需统一评测。", ""]

    indexes = [t["index_coverage"] for t in timings if isinstance(t.get("index_coverage"), dict)]
    if indexes:
        lines += ["## 原文向量覆盖", "",
                  "以下为已作答题源库中可检索及归档原文的索引状态。", "",
                  "- 分段索引轮次：%d / %d；旧前缀索引轮次：%d；分段向量：%d。" % (
                      sum(c.get("chunked_episodes", 0) for c in indexes), sum(c.get("indexed_episodes", 0) for c in indexes),
                      sum(c.get("legacy_prefix_episodes", 0) for c in indexes), sum(c.get("chunk_vectors", 0) for c in indexes)), ""]
    audits = [t["aggregation"] for t in timings if isinstance(t.get("aggregation"), dict)]
    if audits:
        statuses = {name: sum(a.get("status") == name for a in audits)
                    for name in ("checks_passed", "invalid", "not_provided", "unsupported", "disabled")}
        lines += ["## 聚合核验", "",
                  "检查通过 %d；未通过 %d；未提供凭据 %d；操作不支持 %d；关闭 %d；尝试修正 %d。" % (
                      statuses["checks_passed"], statuses["invalid"], statuses["not_provided"], statuses["unsupported"],
                      statuses["disabled"], sum("attempts" in a for a in audits)), "",
                  "未提供凭据包含普通非聚合回答。核验只覆盖引用、单位、显式重复和算术，不能作为语义正确率或证据完整率。", ""]

    lines += ["## 上下文规模", ""]
    ratios = [t["context_tokens"] / t["history_tokens"] for t in timings if t.get("history_tokens")]
    contexts = [t["context_tokens"] for t in timings]
    histories = [t["history_tokens"] for t in timings]
    if timings:
        lines += ["| 指标 | 平均 | 中位数 | P95 |", "|---|---|---|---|",
                  "| 检索记忆 + 问题 token | %.0f | %.0f | %.0f |" % (
                      sum(contexts) / len(contexts), _quantile(contexts, .5), _quantile(contexts, .95)),
                  "| 历史总 token | %.0f | %.0f | %.0f |" % (
                      sum(histories) / len(histories), _quantile(histories, .5), _quantile(histories, .95)),
                  "| 上下文 / 历史 | %.2f | %.2f | %.2f |" % (
                      sum(ratios) / len(ratios) if ratios else 0, _quantile(ratios, .5), _quantile(ratios, .95)),
                  "", "计数单位以各题 budget_counter 为准；旧记录未提供时按历史估算解释。embedding 与重排 token 单独计数。", ""]
        budgeted = [t for t in timings if "memory_tokens" in t and "effective_budget" in t]
        if budgeted:
            lines += ["- 工作记忆超预算：%d / %d 题。" % (
                sum(t["memory_tokens"] > t["effective_budget"] for t in budgeted), len(budgeted)), ""]
        prompts = [t["prompt_tokens_estimate"] for t in timings if "prompt_tokens_estimate" in t]
        if prompts:
            lines += ["- 完整回答提示词（含系统规则）的估算 token：平均 %.0f，中位数 %.0f，P95 %.0f。" % (
                sum(prompts) / len(prompts), _quantile(prompts, .5), _quantile(prompts, .95)), ""]

    lines += ["## 耗时", ""]
    if timings:
        ingest = [t["ingest_s"] for t in timings if not t.get("cached_ingest")]
        answer = [t["answer_s"] for t in timings]
        lines += ["| 阶段 | 题数 | 合计 | 平均 | P95 |", "|---|---|---|---|---|",
                  "| 写入（不含缓存命中） | %d | %.0f s | %.1f s | %.1f s |" % (
                      len(ingest), sum(ingest), sum(ingest) / len(ingest) if ingest else 0, _quantile(ingest, .95)),
                  "| 读取 + 回答 | %d | %.0f s | %.1f s | %.1f s |" % (
                      len(answer), sum(answer), sum(answer) / len(answer), _quantile(answer, .95)),
                  "", "缓存命中的题没有重新写入，所以写入耗时只统计实际写入的题。", ""]
        staged = [t for t in timings if "search_s" in t]
        if staged:
            lines += ["### 读取路径分阶段耗时", "",
                      "| 阶段 | 题数 | 平均 | 中位数 | P95 |", "|---|---|---|---|---|",
                      _stats_row("查询分析（模型）", [t["plan_s"] for t in staged]),
                      _stats_row("**检索**（查询向量化 + 向量 / BM25 + 融合加权）", [t["search_s"] for t in staged],
                                 scale=1000, unit="ms", digits=1),
                      _stats_row("重排（模型）", [t["rerank_s"] for t in staged]),
                      _stats_row("生成回答（模型）", [t["generate_s"] for t in staged]), ""]

    sized = [t for t in timings if "memory_facts" in t]
    if sized:
        totals = [t["memory_facts"] + t["memory_episodes"] for t in sized]
        lines += ["## 检索规模", "",
                  "| 每题记忆条数 | 平均 | 中位数 | P95 | 最大 |", "|---|---|---|---|---|"]
        for name, values in (("事实", [t["memory_facts"] for t in sized]),
                             ("原文轮次", [t["memory_episodes"] for t in sized]), ("合计", totals)):
            lines.append("| %s | %.0f | %.0f | %.0f | %d |" % (
                name, sum(values) / len(values), _quantile(values, .5), _quantile(values, .95), max(values)))
        lines += ["", "| 记忆条数 | 题数 | 检索平均 | 检索 P95 |", "|---|---|---|---|"]
        for low, high in SIZE_BINS:
            group = [t["search_s"] * 1000 for t, n in zip(sized, totals)
                     if n >= low and (high is None or n <= high) and "search_s" in t]
            if group:
                lines.append("| %s | %d | %.1f ms | %.1f ms |" % (
                    "%d–%d" % (low, high) if high else "> %d" % (low - 1), len(group),
                    sum(group) / len(group), _quantile(group, .95)))
        lines += ["", "合成数据上更大规模的检索延迟曲线用 `memsys bench scale` 生成。", ""]

    local = [t.get("reranking",{}) for t in timings if t.get("reranking",{}).get("backend") == "bge"]
    if local:
        rss = [r['process_peak_rss_bytes'] for r in local if r.get('process_peak_rss_bytes') is not None]
        lines += ["## 本地重排资源", "",
                  "- BGE 执行：%d 题；设备/精度/批大小：%s。" % (len(local),
                    ", ".join(sorted({"%s/%s/%s" % (r.get('device'),r.get('dtype'),r.get('batch_size')) for r in local}))),
                  "- 加载及推理耗时中位数 %.3f s，P95 %.3f s；共享队列等待中位数 %.3f s。" % (
                    _quantile([r.get('inference_and_load_s',0) for r in local],.5),
                    _quantile([r.get('inference_and_load_s',0) for r in local],.95),
                    _quantile([r.get('queue_s',0) for r in local],.5)),
                  "- 进程生命周期峰值 RSS：%s；含模型、缓存及并行任务，不等同于单次请求独占内存。" % (
                      "%.1f MiB" % (max(rss)/1048576) if rss else "未测量"),
                  "- 每个输入窗口的模型 token、角色、来源区间和相关性 logit 见 traces；分数不是答案正确概率。", ""]
    degraded = [t for t in timings if t.get('reranking',{}).get('degraded')]
    if degraded:
        lines += ["**本地重排降级：%d 题。严格质量对照应失败，不能计为 BGE 成功。**" % len(degraded), ""]

    lines += ["## Token 与费用", ""]
    usage_path = os.path.join(run_dir, "usage.jsonl")
    if os.path.exists(usage_path):
        ledger = UsageLedger.from_jsonl(usage_path)
        records = ledger.records()
        lines += ["| 环节 | 调用次数 | 输入 token | 其中缓存命中 | 输出 token | 费用 | 模型耗时 |",
                  "|---|---|---|---|---|---|---|"]
        for purpose, row in sorted(ledger.summary("purpose").items()):
            lines.append("| %s | %d | %d | %d | %d | %s | %.0f s |" % (
                purpose, row["calls"], row["prompt_tokens"], row["cached_tokens"], row["completion_tokens"],
                _cost_cell([r for r in records if r.purpose == purpose]), row["latency_s"]))
        total = ledger.totals()
        lines.append("| **合计** | %d | %d | %d | %d | %s | %.0f s |" % (
            total["calls"], total["prompt_tokens"], total["cached_tokens"], total["completion_tokens"],
            _cost_cell(records), total["latency_s"]))
        missing_usage = sum(1 for r in records if not r.usage_reported)
        unknown_cost = sum(1 for r in records if not _known_cost(r))
        per_question = ledger.summary("tag:question_id")
        currencies = {r.pricing.get("currency") for r in records if _known_cost(r) and r.cost}
        average_cost = ("%.5f" % (total["cost"] / max(len(per_question), 1))
                        if records and not unknown_cost and len(currencies) <= 1 else "未知")
        lines += ["", "- 按模型：%s" % ", ".join("%s %d 次" % (m, row["calls"])
                                              for m, row in sorted(ledger.summary("model").items())),
                  "- 每题平均费用：%s（%d 题有记录）" % (average_cost, len(per_question)),
                  "- 服务端没有返回 usage 的调用：%d 次（这些调用的 token 未知，数字字段保留为 0）" % missing_usage,
                  "- 无法完整估算费用的调用：%d 次；历史账本中的零费用不等于免费。" % unknown_cost,
                  "- 费用为按记录单价计算的估算；null 表示价格未知，显式 0 表示免费或本地运行。账本包括续跑和评分调用。",
                  ""]
        prices = {json.dumps(r.pricing, sort_keys=True, ensure_ascii=False) for r in records if r.pricing}
        if prices:
            lines += ["### 使用的价格记录", ""]
            for price in sorted(prices):
                lines.append("- %s" % price)
            lines.append("")
        known_currencies = {r.pricing.get("currency") or "未记录币种" for r in records if _known_cost(r)}
        for currency in sorted(known_currencies):
            amount = sum(r.cost for r in records if _known_cost(r)
                         and (r.pricing.get("currency") or "未记录币种") == currency)
            lines.append("- 已知费用部分（%s）：%.4f" % (currency, amount))
        if known_currencies:
            lines.append("")
    else:
        lines += ["没有找到 usage.jsonl。", ""]

    if labels:
        lines += ["## 错题（用于失败分析）", ""]
        for qtype in TYPE_ORDER:
            wrong = [q for q in labels if q in golds and golds[q].question_type == qtype and not labels[q]]
            if wrong:
                lines.append("- %s（%d）：%s" % (qtype, len(wrong), ", ".join(sorted(wrong)[:15])
                                                + (" …" if len(wrong) > 15 else "")))
        lines += ["", "每题的检索轨迹在 `traces/<题号>.json`：查询计划、候选及分数、选入上下文的记忆、笔记和回答。", ""]
    return "\n".join(lines)


def _judge_model(eval_log: str) -> str:
    for record in _read_jsonl(eval_log)[:1]:
        return str(record.get("autoeval_label", {}).get("model", "?"))
    return "?"
