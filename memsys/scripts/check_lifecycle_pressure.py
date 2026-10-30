"""合成记忆的容量、保留、归档召回与预算压力检查；不调用回答或抽取模型。"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

from memsys.bench.provenance import prompt_fingerprint, source_manifest
from memsys.budget import budget_counter
from memsys.bench.scale import build_store
from memsys.config import MemorySettings, Models
from memsys.fake import HashEmbedder
from memsys.lifecycle import run_lifecycle
from memsys.read import QueryPlan, Retriever, fit_to_budget
from memsys.schema import ARCHIVED, FACT_CURRENT, FACT_SUPERSEDED
from memsys.textutil import estimate_tokens


def measure(sizes, cap=32):
    now = datetime(2025, 5, 4, 10)
    settings = MemorySettings(max_active_facts=cap, max_active_episodes=cap,
                              query_analysis=False, rerank_backend="fusion", min_similarity=0.8,
                              context_budget_tokens=600, max_history_share=0)
    embed = HashEmbedder(dim=1024)
    models = Models(chat=None, memory=None, embedder=embed, embedding_name="hash-1024")
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        for size in sizes:
            store = build_store(str(Path(directory) / (str(size) + ".sqlite")), size, embed)
            try:
                store.check_embedding_model(models.embedding_name)
                with store.transaction():
                    extra = []
                    for text, born, importance in [
                        ("The user keeps artifactzephyr at the observatory.", datetime(1900, 1, 1), .1),
                        ("The user keeps artifactaurora in the studio.", now, 1.0)]:
                        extra.append(store.add_fact(text=text, kind="personal", subject="user", attribute="artifact",
                            value=text, event=None, recorded_at=born, valid_from=None,
                            session_id="s1", importance=importance))
                    store.put_vectors("fact", list(zip(extra, embed.embed([
                        "The user keeps artifactzephyr at the observatory.", "The user keeps artifactaurora in the studio."]))))
                started = time.monotonic()
                run_lifecycle(store, now, settings)
                lifecycle_ms = 1000 * (time.monotonic() - started)
                second = run_lifecycle(store, now, settings)
                active_facts = len(store.facts(statuses=(FACT_CURRENT, FACT_SUPERSEDED)))
                active_episodes = len(store.episodes(statuses=("active",)))
                plan = QueryPlan(["artifactzephyr"], ["artifactzephyr"], None, False, "past", "synthetic")
                retrieval = Retriever(store, models, settings)
                started = time.monotonic()
                result = retrieval.search(plan)
                selection = retrieval.select(result, plan, today=now.date())
                memory = fit_to_budget(selection, now.date(), 600, counter=budget_counter(settings))
                search_ms = 1000 * (time.monotonic() - started)
                checks = {"capacity_bounded": active_facts <= cap and active_episodes <= cap,
                          "valuable_fact_retained": store.fact(extra[1]).status == FACT_CURRENT,
                          "old_fact_archived": store.fact(extra[0]).status == ARCHIVED,
                          "archived_fact_recalled": "artifactzephyr" in memory and result.used_archive,
                          "budget_bounded": budget_counter(settings).count(memory) <= 600,
                          "maintenance_idempotent": not second.archived_facts and not second.archived_episodes}
                rows.append({"synthetic_items": size + 2, "active_facts": active_facts,
                             "active_episodes": active_episodes, "lifecycle_ms": round(lifecycle_ms, 2),
                             "archive_search_and_selection_ms": round(search_ms, 2), "checks": checks,
                             "passed": all(checks.values())})
            finally:
                store.close()
    return rows, settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--sizes", default="100,1000,10000")
    args = parser.parse_args()
    sizes = [int(n) for n in args.sizes.split(",")]
    if any(n < 2 for n in sizes):
        raise ValueError("sizes must be at least 2")
    rows, settings = measure(sizes)
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    report = {"mode": "synthetic lifecycle pressure; no model generation; not a benchmark score",
              "settings": settings.to_dict(), "rows": rows, "passed": all(r["passed"] for r in rows),
              "provenance": source_manifest(), "prompt_fingerprint": prompt_fingerprint(),
              "validation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for row in rows:
        print("Items %d; active facts/episodes %d/%d; maintenance %.2f ms; archive search %.2f ms; passed %s" % (
            row["synthetic_items"], row["active_facts"], row["active_episodes"], row["lifecycle_ms"],
            row["archive_search_and_selection_ms"], row["passed"]))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
