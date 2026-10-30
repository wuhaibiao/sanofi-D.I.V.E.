"""使用真实 embedding 和自拟事实检查跨语言召回；抽取用替身，不生成答案或评测分数。"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from datetime import datetime
from pathlib import Path

from memsys.bench.provenance import model_details, prompt_fingerprint, source_manifest
from memsys.config import AppConfig, Models
from memsys.core import MemorySystem
from memsys.fake import ScriptedChat
from memsys.llm import UsageLedger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ledger = UsageLedger(str(out / "usage.jsonl"))
    config = AppConfig.load(args.config)
    embedder = config.client("embedding", ledger)
    items = [("I grow a dwarf lime tree in a planter on my balcony.", "balcony_plant"),
             ("I ride a bicycle to my office every weekday.", "commute"),
             ("I collect old vinyl jazz records as a hobby.", "hobby")]
    replies = [json.dumps({"facts": [{"text": text, "kind": "personal", "subject": "user",
                                      "attribute": slot, "value": text, "rounds": [1],
                                      "importance": 3, "certainty": "firm"}]}) for text, slot in items]
    tails = [("User", "I use a handheld thermal camera to locate heat leaks around the windows.",
              "我用什么仪器寻找窗户漏热的位置？"),
             ("Assistant", "For evening practice, I recommend a bass clarinet.",
              "你之前推荐我晚上练习哪种管乐器？")]
    scripted_facts = iter(replies)
    # 长原文在严格预算下产生多个抽取分段；这些分段都不提供事实捷径。
    model = ScriptedChat({"extract": lambda messages: next(scripted_facts, '{"facts": []}'),
                          "resolve": '{"decisions": []}'})
    models = Models(chat=None, memory=model, embedder=embedder,
                    embedding_name=embedder.config.model + "@isolated-validation")
    settings = dataclasses.replace(config.memory, query_analysis=False, rerank_backend="fusion",
                                   max_history_share=0, context_budget_tokens=600)
    system = MemorySystem(models, settings)
    checks = []
    try:
        for index, (text, _) in enumerate(items):
            at = datetime(2025, 1, index + 5, 10)
            handle = system.open_session("local-validation", at)
            system.add_turn(handle, "user", text, at)
            system.add_turn(handle, "assistant", "Thanks for the update.", at)
            system.close_session(handle, at)
        # 将尾部放在字符分段边界之后，两种说话人都覆盖；没有抽取事实作为捷径。
        filler = "I recorded the shelving, curtain colors and dimensions of the living room. "
        prefix_length = settings.embed_chars * 3
        prefix = (filler * (prefix_length // len(filler) + 1))[:prefix_length]
        for index, (role, tail, _) in enumerate(tails):
            at = datetime(2025, 1, index + 10, 10)
            handle = system.open_session("local-validation", at)
            system.add_turn(handle, "user", prefix + "\n" + tail if role == "User" else "Please suggest an instrument.", at)
            if role == "Assistant":
                system.add_turn(handle, "assistant", prefix + "\n" + tail, at)
            system.close_session(handle, at)
        dimensions = len(system.store("local-validation").vectors("fact", ("current",))[1][0])
        for question, expected in [("我在阳台上养的是什么植物？", "F1"),
                                   ("How do I get to my workplace on weekdays?", "F2")]:
            trace = system.recall("local-validation", question, datetime(2025, 2, 1), save_trace=False).trace
            facts = [row for row in trace["candidates"] if row["key"].startswith("F")]
            checks.append({"query": question, "expected_top_fact": expected,
                           "top_fact": facts[0]["key"] if facts else None,
                           "passed": bool(facts) and facts[0]["key"] == expected,
                           "memory_tokens": trace["memory_tokens"], "effective_budget": trace["effective_budget"],
                           "within_budget": trace["memory_tokens"] <= trace["effective_budget"],
                           "fact_similarities": {row["key"]: row["sim"] for row in facts}})
        tail_checks = []
        for index, (role, tail, question) in enumerate(tails):
            recall = system.recall("local-validation", question, datetime(2025, 2, 1), save_trace=False)
            expected = "E%d" % (len(items) + index + 1)
            row = next((r for r in recall.trace["candidates"] if r["key"] == expected), None)
            tail_checks.append({"query": question, "role": role, "expected_episode": expected,
                                "tail": tail, "tail_in_selected_memory": tail in recall.memory,
                                "selected_memory": recall.memory,
                                "selected": recall.trace["selected"],
                                "passed": tail in recall.memory and row is not None and bool(row["matched_chunks"]),
                                "matched_chunks": row["matched_chunks"] if row else [],
                                "memory_tokens": recall.trace["memory_tokens"],
                                "effective_budget": recall.trace["effective_budget"],
                                "within_budget": recall.trace["memory_tokens"] <= recall.trace["effective_budget"]})
        chunk_count = len(system.store("local-validation").vectors("episode_chunk", ("active",))[0])
        # 同一个原文库关闭分段后重建：只比较检索输入和片段，仍不生成回答或使用评分标签。
        system.settings.episode_chunk_vectors = False
        system.reindex("local-validation")
        for row, (_, tail, question) in zip(tail_checks, tails):
            recall = system.recall("local-validation", question, datetime(2025, 2, 1), save_trace=False)
            row["legacy_prefix_tail_in_memory"] = tail in recall.memory
        records = ledger.records()
        report = {"mode": "self-authored cross-language retrieval smoke; scripted extraction; real embeddings; no answers or benchmark scores",
                  "model": model_details(embedder), "embedding_dimensions": dimensions, "checks": checks,
                  "tail_checks": tail_checks, "chunk_vectors": chunk_count,
                  "embedding_calls": len(records),
                  "all_costs_known_free": bool(records) and all(r.cost_known is True and r.cost == 0 for r in records),
                  "provenance": source_manifest(), "prompt_fingerprint": prompt_fingerprint(),
                  "validation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Embedding dimensions: %d; retrieval checks passed: %d / %d; known free: %s" % (
            dimensions, sum(c["passed"] and c["within_budget"] for c in checks), len(checks), report["all_costs_known_free"]))
        print("Cross-language long-tail checks passed: %d / %d; prefix mode retained tails: %d / %d" % (
            sum(c["passed"] and c["within_budget"] for c in tail_checks), len(tail_checks),
            sum(c["legacy_prefix_tail_in_memory"] for c in tail_checks), len(tail_checks)))
        return 0 if all(c["passed"] and c["within_budget"] for c in checks + tail_checks) else 1
    finally:
        system.close()


if __name__ == "__main__":
    raise SystemExit(main())
