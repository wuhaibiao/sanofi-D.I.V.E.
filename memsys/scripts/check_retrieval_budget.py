"""在已有哈希向量记忆库的只读副本上核验预算；不调用模型、不生成或评分答案。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path

from memsys.bench.longmemeval import load
from memsys.bench.provenance import prompt_fingerprint, source_manifest
from memsys.config import MemorySettings, Models
from memsys.core import MemorySystem, _safe_name
from memsys.fake import HashEmbedder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--stores", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    settings = MemorySettings(query_analysis=False, rerank_backend="fusion")
    with tempfile.TemporaryDirectory() as tmp:
        for item, _ in load(args.data):
            name = _safe_name(item.question_id) + ".sqlite"
            original = Path(args.stores) / name
            if not original.exists():
                continue
            target = Path(tmp) / name
            source = sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
                meta = dict(destination.execute("SELECT key,value FROM meta"))
            finally:
                destination.close()
                source.close()
            if meta.get("ingested") != "1":
                target.unlink()
                continue
            embedding = meta.get("embedding_model", "")
            match = re.match(r"hash-(\d+)", embedding)
            if not match:
                raise ValueError("This offline check requires hash embeddings; got %s" % embedding)
            models = Models(chat=None, memory=None, embedder=HashEmbedder(dim=int(match.group(1))),
                            embedding_name=embedding)
            system = MemorySystem(models, settings, data_dir=tmp)
            try:
                trace = system.recall(item.question_id, item.question, item.question_date, save_trace=False).trace
                rows.append({"question_id": item.question_id, "memory_tokens": trace["memory_tokens"],
                    "effective_budget": trace["effective_budget"], "context_tokens": trace["context_tokens"],
                    "history_tokens": trace["history_tokens"],
                    "within_budget": trace["memory_tokens"] <= trace["effective_budget"],
                    "above_history": trace["context_tokens"] > trace["history_tokens"]})
            finally:
                system.close()
            target.unlink()
    summary = {"mode": "retrieval-only; existing hash embeddings; query analysis and rerank disabled; no answer or grade",
        "stores_replayed": len(rows), "budget_violations": sum(not r["within_budget"] for r in rows),
        "above_history": sum(r["above_history"] for r in rows),
        "above_history_with_history_at_least_300": sum(r["above_history"] and r["history_tokens"] >= 300 for r in rows),
        "settings": settings.to_dict(), "provenance": source_manifest(),
        "prompt_fingerprint": prompt_fingerprint(),
        "validation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output / "retrieval-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")
    (output / "retrieval-checks.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    print("Checked %d stores; budget violations: %d; above history (>=300 tokens): %d" % (
        len(rows), summary["budget_violations"], summary["above_history_with_history_at_least_300"]))
    return 1 if summary["budget_violations"] or not rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
