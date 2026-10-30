"""Read-only verification and presentation of an already finished trace.

No model calls, source rewriting, rescoring, or answer regeneration.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import zipfile


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    frozen, inputs, status = (read(out / name) for name in ("freeze.json", "input.json", "status.json"))
    if status["status"] != "completed":
        raise ValueError("Keep failed attempts separate; export requires a completed run")
    for relative, expected in frozen["source_files"].items():
        assert sha(out / "frozen-src" / relative) == expected, relative
    assert sha(out / "config.frozen.json") == frozen["config_sha256"]
    assert sha(out / "source-case.no-label.json") == frozen["case_sha256"]
    assert "answer" not in read(out / "source-case.no-label.json")[0]
    expected = inputs["sessions"]
    with sqlite3.connect("file:" + str(out / "pre-answer.sqlite") + "?mode=ro", uri=True) as db:
        actual = db.execute("SELECT id,external_id,opened_at,closed_at FROM sessions").fetchall()
        ids = {row[1]: row for row in actual}
        assert len(ids) == len(actual) == len(expected)
        turns = 0
        for session in expected:
            row = ids[session["session_id"]]
            assert row[2] == row[3] == session["date"]
            rows = db.execute("SELECT role,content,at FROM turns WHERE session_id=? ORDER BY idx", (row[0],)).fetchall()
            assert rows == [(turn["role"], turn["content"], session["date"]) for turn in session["turns"]]
            turns += len(rows)
        dangling = db.execute("SELECT COUNT(*) FROM fact_sources s LEFT JOIN facts f ON s.fact_id=f.id "
                              "LEFT JOIN episodes e ON s.episode_id=e.id WHERE f.id IS NULL OR e.id IS NULL").fetchone()[0]
        assert dangling == 0
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    answered = next(row for row in events if row["kind"] == "answer_stage_end")
    label = next(row for row in events if row["kind"] == "reference_label_access_start")
    assert label["event"] > answered["event"]
    assert label["sealed_answer_sha256"] == sha(out / "run" / "hypotheses.jsonl")
    calls = [read(path) for path in sorted((out / "calls").glob("*.json"))]
    http = [read(path) for path in sorted((out / "http").glob("*.json"))]
    assert all(row["success"] for row in calls)
    assert all(row.get("response", {}).get("status") == 200 for row in http)
    assert all(row["call_id"] > label["event"] for row in calls if row["context"]["purpose"] == "judge")
    assert all(value == "[REDACTED]" for row in http for key, value in row["request"]["headers"].items()
               if key.lower() in ("authorization", "x-api-key"))
    # Check actual credential literals, without ever printing them.
    from memsys.cli import load_dotenv
    load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))
    secrets = [v.encode() for k, v in os.environ.items()
               if len(v) >= 12 and (k.endswith("_API_KEY") or k.endswith("_TOKEN"))]
    checked = 0
    for path in out.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".html", ".log", ".txt"):
            content = path.read_bytes()
            assert not any(secret in content for secret in secrets), "Credential literal in " + str(path)
            checked += 1
    chat = [row for row in calls if row["method"] == "chat"]
    audit = {"status": "passed", "original_sessions": len(expected), "original_turns": turns,
             "roles_dates_contents_exact": True, "dangling_fact_sources": dangling,
             "label_access_after_answer": True, "answer_unchanged_after_judging": True,
             "frozen_source_and_config_exact": True, "all_http_attempts_successful": True,
             "http_attempts": len(http), "logical_model_calls": len(calls), "cloud_chat_calls": len(chat),
             "authentication_headers_redacted": True, "credential_literal_scan_files": checked,
             "response_replay": False, "benchmark_correct": status["correct"],
             "note": "Source integrity checks are not proof of semantic correctness or absence of benchmark overfitting"}
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    sections = ["跨会话单题：全部原始会话与云端模型输入输出\n",
                "这是一份已完成运行的记录，不是盲测。没有据评分重跑或改系统规则。\n",
                "完整向量、HTTP请求响应、数据库、BGE配对文本和分数见 report.html 及 full-trace.zip。\n",
                "问题：" + inputs["question"] + "\n", "审计：\n" + json.dumps(audit, ensure_ascii=False, indent=2),
                "运行状态与用量：\n" + json.dumps(status, ensure_ascii=False, indent=2)]
    for number, session in enumerate(expected, 1):
        sections.append(f'\n会话 {number} / {session["date"]} / {session["session_id"]}\n')
        for index, turn in enumerate(session["turns"], 1):
            sections.append(f'[{index:02d} {turn["role"]}]\n{turn["content"]}\n')
        report = out / "states" / f"session-{number:02d}-report.json"
        sections.append("写入与生命周期报告：\n" + report.read_text())
    sections.append("\n所有云端模型调用（按发生次序；包括程序证据校验触发的第二次回答）\n")
    for row in chat:
        sections.append(f'\n调用 {row["call_id"]} / {row["context"]["purpose"]}\n' + json.dumps(row, ensure_ascii=False, indent=2))
    reply = read(out / "reply.json")
    sections.append("\n最终回答：\n" + reply["text"])
    sections.append("\n完整检索、重排、分页和验证trace：\n" + json.dumps(reply["trace"], ensure_ascii=False, indent=2))
    sections.append("\n评分专用参考标签（仅回答完成后读取）：\n" + (out / "reference-answer.judge-only.json").read_text())
    (out / "transcript.txt").write_text("\n".join(sections))
    archive = out / "full-trace.zip"
    if archive.exists():
        raise ValueError("Do not overwrite an earlier archive")
    paths = [path for path in sorted(out.rglob("*")) if path.is_file() and
             "__pycache__" not in path.parts and path.suffix != ".pyc"]
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            bundle.write(path, path.relative_to(out))
    print(json.dumps({"audit": audit, "transcript_bytes": (out / "transcript.txt").stat().st_size,
                      "archive_bytes": archive.stat().st_size, "archive": str(archive)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
