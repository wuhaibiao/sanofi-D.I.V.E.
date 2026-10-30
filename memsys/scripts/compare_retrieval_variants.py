"""Summarize completed frozen comparisons without changing answers or judge labels."""

from __future__ import annotations

import argparse
import collections
import json
import math
from pathlib import Path


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    # Match memsys.bench.report and the predeclared resource gates exactly.
    return values[min(round((len(values) - 1) * fraction), len(values) - 1)]


def summarize(directory, golds):
    status = json.loads((directory / "status.json").read_text())
    if status["status"] != "complete":
        raise ValueError("Comparison is incomplete: " + str(directory))
    evaluation = list(directory.glob("hypotheses.jsonl.eval-results-*"))
    if len(evaluation) != 1:
        raise ValueError("Expected exactly one fixed judge result: " + str(directory))
    labels = {r["question_id"]: bool(r["autoeval_label"]["label"]) for r in rows(evaluation[0])}
    answers = {r["question_id"]: r["hypothesis"] for r in rows(directory / "hypotheses.jsonl")}
    timings = rows(directory / "timings.jsonl")
    if set(labels) != set(golds) or set(answers) != set(golds) or {r["question_id"] for r in timings} != set(golds):
        raise ValueError("Question identities differ: " + str(directory))
    usage = rows(directory / "usage.jsonl")
    answer_usage = [r for r in usage if r["purpose"] != "judge" and not r["purpose"].startswith("embed")]
    if any(r["purpose"] in ("extract", "resolve") for r in usage):
        raise ValueError("Unexpected factual write call")
    purposes = collections.defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cached_input": 0})
    for r in usage:
        value = purposes[r["purpose"]]
        value["calls"] += 1
        value["input"] += r["prompt_tokens"]
        value["output"] += r["completion_tokens"]
        value["cached_input"] += r["cached_tokens"]
    types = collections.defaultdict(lambda: {"correct": 0, "total": 0})
    for qid, label in labels.items():
        kind = golds[qid].get("question_type", "unknown")
        types[kind]["total"] += 1
        types[kind]["correct"] += int(label)
    abstentions = [qid for qid in golds if qid.endswith("_abs")]
    reranking = [r.get("reranking", {}) for r in timings]
    coverage, audits, planners = collections.Counter(), collections.Counter(), collections.Counter()
    planner_errors, retrieval_errors = [], []
    for qid in golds:
        trace = json.loads((directory / "traces" / (qid + ".json")).read_text())
        c = trace.get("coverage", {})
        if c.get("needs_all"):
            coverage["complete_search" if c.get("search_complete") else "incomplete_search"] += 1
        audits[str(trace.get("aggregation", {}).get("status"))] += 1
        planners[trace.get("plan", {}).get("source", "unknown")] += 1
        if trace.get("plan", {}).get("error"):
            planner_errors.append(qid)
        if trace.get("retrieval_errors"):
            retrieval_errors.append(qid)
    summary = {
        "correct": sum(labels.values()), "total": len(labels), "by_type": dict(types),
        "abstention_correct": sum(labels[qid] for qid in abstentions), "abstention_total": len(abstentions),
        "answer_cloud_calls": len(answer_usage),
        "answer_cloud_input_tokens": sum(r["prompt_tokens"] for r in answer_usage),
        "answer_cloud_output_tokens": sum(r["completion_tokens"] for r in answer_usage),
        "answer_cloud_tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in answer_usage),
        "usage_by_purpose": dict(purposes), "all_requests": len(usage),
        "answer_p50_s": percentile([r["answer_s"] for r in timings], .5),
        "answer_p95_s": percentile([r["answer_s"] for r in timings], .95),
        "answer_max_s": max(r["answer_s"] for r in timings),
        "reindex_p50_s": percentile([r["ingest_s"] for r in timings], .5),
        "reindex_p95_s": percentile([r["ingest_s"] for r in timings], .95),
        "rerank_queue_p95_s": percentile([r["queue_s"] for r in reranking if "queue_s" in r], .95),
        "rerank_inference_load_p95_s": percentile([r["inference_and_load_s"] for r in reranking if "inference_and_load_s" in r], .95),
        "process_lifetime_peak_rss_bytes": max([r["process_peak_rss_bytes"] for r in reranking if r.get("process_peak_rss_bytes") is not None], default=None),
        "max_reported_mps_current_allocated_bytes": max([r["device_allocated_bytes"] for r in reranking if r.get("device_allocated_bytes") is not None], default=None),
        "rerank_degradations": sum(bool(r.get("degraded")) for r in reranking),
        "coverage_audit": dict(coverage), "program_audit_statuses": dict(audits),
        "planner_sources": dict(planners), "planner_error_questions": planner_errors,
        "retrieval_error_questions": retrieval_errors,
        "prices_complete": all(r.get("cost_known") is True for r in answer_usage),
        "usage_missing_calls": sum(not r.get("usage_reported") for r in answer_usage),
        "database_file_bytes": sum(p.stat().st_size for p in (directory / "stores").glob("*.sqlite")),
        "frozen_fact_stores": json.loads((directory / "freeze.json").read_text())["frozen_fact_stores"],
    }
    return summary, labels, answers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--sample-root", type=Path, default=Path("runs/semantic-repair-v16"))
    args = parser.parse_args()
    result = {"scope": "Previously used regression samples; retrieval isolation; no monetary estimate without complete prices.",
              "quantile": "sorted values at round(q*(n-1)); same as memsys.bench.report and resource gates", "variants": {}}
    for group, sample_dir in (("fixed34", "fixed34"), ("used14", "holdout14")):
        sample = json.loads((args.sample_root / sample_dir / "sample.json").read_text())
        golds = {r["question_id"]: r for r in sample}
        baseline = None
        for variant in args.variants:
            directory = args.root / variant / group
            if not (directory / "status.json").exists():
                continue
            summary, labels, answers = summarize(directory, golds)
            fact_stores = summary.pop("frozen_fact_stores")
            if baseline is None:
                baseline = (labels, answers, summary, json.loads((directory / "freeze.json").read_text()))
            else:
                if fact_stores != baseline[3]["frozen_fact_stores"]:
                    raise ValueError("Fact stores differ between paired runs")
                changes = []
                for qid in golds:
                    if baseline[0][qid] != labels[qid]:
                        changes.append({"question_id": qid, "type": golds[qid].get("question_type"),
                            "direction": "improved" if labels[qid] else "regressed",
                            "question": golds[qid]["question"], "reference": golds[qid].get("answer"),
                            "baseline_answer": baseline[1][qid], "variant_answer": answers[qid]})
                summary["paired_changes"] = changes
                summary["answer_token_change_pct"] = 100 * (summary["answer_cloud_tokens"] / baseline[2]["answer_cloud_tokens"] - 1)
            result["variants"].setdefault(variant, {})[group] = summary
    result["combined"] = {}
    for variant, groups in result["variants"].items():
        if set(groups) != {"fixed34", "used14"}:
            continue
        combined = {key: sum(s[key] for s in groups.values()) for key in
            ("correct", "total", "answer_cloud_calls", "answer_cloud_input_tokens", "answer_cloud_output_tokens", "answer_cloud_tokens")}
        combined["accuracy_pct"] = 100 * combined["correct"] / combined["total"]
        changes = [r for s in groups.values() for r in s.get("paired_changes", [])]
        combined["paired_improved"] = sum(r["direction"] == "improved" for r in changes)
        combined["paired_regressed"] = sum(r["direction"] == "regressed" for r in changes)
        if changes:
            n = len(changes)
            smaller = min(combined["paired_improved"], combined["paired_regressed"])
            combined["mcnemar_exact_two_sided_p"] = min(1, 2 * sum(math.comb(n, k) for k in range(smaller + 1)) / (2 ** n))
        timings = [r for group in groups for r in rows(args.root / variant / group / "timings.jsonl")]
        combined["answer_p50_s"] = percentile([r["answer_s"] for r in timings], .5)
        combined["answer_p95_s"] = percentile([r["answer_s"] for r in timings], .95)
        combined["answer_max_s"] = max(r["answer_s"] for r in timings)
        result["combined"][variant] = combined
    (args.root / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for variant, groups in result["variants"].items():
        for group, s in groups.items():
            print(f"{variant}/{group}: {s['correct']}/{s['total']}; answer tokens {s['answer_cloud_tokens']}; p95 {s['answer_p95_s']:.2f}s")


if __name__ == "__main__":
    main()
