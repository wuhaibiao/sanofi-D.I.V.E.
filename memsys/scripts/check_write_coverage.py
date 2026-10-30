"""核验历史原文的分段覆盖与抽取输入预算；使用空输出替身，不调用模型或写入记忆库。"""

from __future__ import annotations

import argparse
from contextlib import closing
import dataclasses
import hashlib
import json
from pathlib import Path

from memsys.bench.longmemeval import load
from memsys.bench.provenance import prompt_fingerprint, source_manifest
from memsys.budget import budget_counter
from memsys.config import AppConfig, MemorySettings, Models
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.indexing import episode_index
from memsys.store import Store
from memsys.textutil import estimate_tokens
from memsys.write import Round, WriteReport, Writer, build_rounds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--config")
    parser.add_argument("--prefix", action="store_true", help="使用旧助手前缀模式做对照")
    args = parser.parse_args()
    settings = AppConfig.load(args.config).memory if args.config else MemorySettings()
    if args.prefix:
        settings = dataclasses.replace(settings, extract_full_rounds=False)
    rows = []
    counter = budget_counter(settings)
    with closing(Store()) as store:
        model = ScriptedChat({"extract": '{"facts": []}'})
        writer = Writer(store, Models(chat=None, memory=model, embedder=HashEmbedder(),
                                      embedding_name="hash-256"), settings)
        for item, _ in load(args.data):  # GoldLabel 不传给 Writer。
            report = WriteReport(item.question_id)
            lossless_errors = input_budget_errors = 0
            embedding_chunks = embedding_tokens = embedding_lossless_errors = embedding_budget_errors = 0
            embedding_max_tokens = 0
            for session in item.sessions:
                turns = [{"at": session.date.isoformat(), "role": t.role, "content": t.content}
                         for t in session.turns]
                rounds = [Round(i + 1, i, at, u, a) for i, (at, u, a) in enumerate(build_rounds(turns))]
                index = episode_index([(r.user_text, r.assistant_text) for r in rounds], settings)
                embedding_chunks += len(index.texts)
                token_counts = [counter.count(text) for text in index.texts]
                embedding_tokens += sum(token_counts)
                embedding_max_tokens = max([embedding_max_tokens] + token_counts)
                if settings.episode_chunk_vectors:
                    embedding_budget_errors += sum(count > settings.embed_chunk_tokens for count in token_counts)
                    for n, r in enumerate(rounds):
                        for role, text in (("User", r.user_text), ("Assistant", r.assistant_text)):
                            chunks = [c for c in index.chunks.get(n, []) if c.role == role]
                            restored = "".join(index.texts[c.vector_index][len(role) + 2:] for c in chunks)
                            embedding_lossless_errors += restored != text
                report.rounds += len(rounds)
                for r in rounds:
                    parts = writer._parts(r, WriteReport("preview"))
                    if settings.extract_full_rounds:
                        assistants = "".join(p.assistant_text for p in parts)
                        user_only = [p.user_text for p in parts if p.user_text and not p.assistant_text]
                        users = [p.user_text for p in parts if p.user_text]
                        user_complete = ("".join(user_only) == r.user_text if user_only
                                         else all(u == r.user_text for u in users) and (bool(users) or not r.user_text))
                        lossless_errors += assistants != r.assistant_text or not user_complete
                    input_budget_errors += sum(counter.count(p.render()) > settings.extract_chunk_tokens for p in parts)
                writer._extract(rounds, session.date, report)
                model.calls.clear()
            rows.append({"question_id": item.question_id, "rounds": report.rounds,
                         "parts": report.extract_parts, "split_rounds": report.extract_split_rounds,
                         "truncated_rounds": report.extract_truncated_rounds, "calls": report.extract_calls,
                         "max_dialogue_tokens": report.extract_max_input_tokens,
                         "dialogue_input_tokens": report.extract_input_tokens,
                         "lossless_errors": lossless_errors, "input_budget_errors": input_budget_errors})
            rows[-1].update(embedding_chunks=embedding_chunks, embedding_tokens=embedding_tokens,
                            embedding_max_tokens=embedding_max_tokens, embedding_lossless_errors=embedding_lossless_errors,
                            embedding_budget_errors=embedding_budget_errors)
    totals = {key: sum(r[key] for r in rows) for key in ("rounds", "parts", "split_rounds", "truncated_rounds",
              "calls", "dialogue_input_tokens", "lossless_errors", "input_budget_errors", "embedding_chunks",
              "embedding_tokens", "embedding_lossless_errors", "embedding_budget_errors")}
    summary = {"mode": "ungated write input validation; empty-output scripted model; no facts, answers, or scores",
               "questions": len(rows), **totals,
               "lossless_checked": settings.extract_full_rounds,
               "embedding_lossless_checked": settings.episode_chunk_vectors,
               "embedding_budget_checked": settings.episode_chunk_vectors,
               "embedding_max_tokens": max((r["embedding_max_tokens"] for r in rows), default=0),
               "max_dialogue_tokens": max((r["max_dialogue_tokens"] for r in rows), default=0),
               "settings": settings.to_dict(), "provenance": source_manifest(),
               "budget_counter": counter.manifest(),
               "prompt_fingerprint": prompt_fingerprint(),
               "validation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if not settings.extract_full_rounds:
        summary["lossless_errors"] = None  # 前缀模式有意截断，不声称完成了无损检查。
    if not settings.episode_chunk_vectors:
        summary["embedding_lossless_errors"] = None
        summary["embedding_budget_errors"] = None
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "checks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print("Checked %d questions, %d rounds; lossless errors: %s; input budget errors: %d; max: %d tokens" % (
        len(rows), totals["rounds"], str(totals["lossless_errors"]) if settings.extract_full_rounds else "not checked (prefix mode)",
        totals["input_budget_errors"], summary["max_dialogue_tokens"]))
    print("Embedding inputs: %d; estimated tokens: %d; max tokens: %d; lossless checked: %s; budget errors: %s" % (
        totals["embedding_chunks"], totals["embedding_tokens"], summary["embedding_max_tokens"],
        settings.episode_chunk_vectors, str(totals["embedding_budget_errors"]) if settings.episode_chunk_vectors else "not checked (prefix mode)"))
    return 1 if any(totals[key] for key in ("lossless_errors", "input_budget_errors", "embedding_lossless_errors",
                                          "embedding_budget_errors")) or not rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
