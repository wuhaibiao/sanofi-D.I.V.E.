"""Observe one normal memsys replay without tuning or replaying model responses.

Prepare selects the first multi-session entry in source-file order, strips its
label, and freezes the current source/config. Run uses a new store and records
client inputs/outputs, HTTP bytes actually consumed, per-session database
snapshots, retrieval traces, and (only after answering) the separate judge.
"""
from __future__ import annotations

import argparse
import base64
import contextvars
import dataclasses
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = contextvars.ContextVar("trace_one_context", default={})


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def serial(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "base64", "bytes": len(value),
                "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=serial) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text())


def prepare(args):
    if args.out.exists():
        raise ValueError("Use a new output directory; never overwrite an earlier attempt")
    entries = read_json(args.data)
    selected = next(row for row in entries if row["question_type"] == "multi-session")
    allowed = ("question_id", "question_type", "question", "question_date",
               "haystack_session_ids", "haystack_dates", "haystack_sessions")
    case = {key: selected[key] for key in allowed}
    args.out.mkdir(mode=0o700, parents=True)
    shutil.copytree(ROOT / "src" / "memsys", args.out / "frozen-src" / "memsys",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The config contains environment variable names, not credential values.
    shutil.copyfile(args.config, args.out / "config.frozen.json")
    write_json(args.out / "source-case.no-label.json", [case])
    sys.path.insert(0, str(args.out / "frozen-src"))
    from memsys.bench.longmemeval import load
    from memsys.bench.provenance import source_manifest, prompt_fingerprint
    item, _ = load(str(args.out / "source-case.no-label.json"))[0]
    write_json(args.out / "input.json", item)
    write_json(args.out / "freeze.json", {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "selection": "First multi-session entry in original file order; no correctness-based selection",
        "question_id": item.question_id, "source_data": str(args.data.resolve()),
        "source_data_sha256": sha(args.data), "config_sha256": sha(args.out / "config.frozen.json"),
        "observer_sha256": sha(__file__),
        "case_sha256": sha(args.out / "source-case.no-label.json"),
        "source": source_manifest(), "prompts": prompt_fingerprint(),
        "source_files": {str(p.relative_to(args.out / "frozen-src")): sha(p)
                         for p in sorted((args.out / "frozen-src").rglob("*.py"))},
        "limitations": "This previously used benchmark is a demonstration, not a blind generalization test",
        "settings_changes": [], "response_replay": False, "mem0_execution": False,
    })
    print(json.dumps({"status": "prepared", "question_id": item.question_id,
                      "sessions": len(item.sessions), "turns": sum(len(s.turns) for s in item.sessions),
                      "output": str(args.out)}, ensure_ascii=False), flush=True)


class Recorder:
    def __init__(self, out):
        self.out = out
        self.lock = threading.Lock()
        self.sequence = 0
        self.secrets = {v for k, v in os.environ.items()
                        if len(v) >= 12 and (k.endswith("_API_KEY") or k.endswith("_TOKEN"))}
        for name in ("calls", "http", "states"):
            (out / name).mkdir(exist_ok=True)

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            sensitive = {"authorization", "proxy-authorization", "x-api-key", "api_key",
                         "apikey", "cookie", "set-cookie", "access_token", "refresh_token"}
            return {k: "[REDACTED]" if str(k).lower().replace("_", "-") in
                    {s.replace("_", "-") for s in sensitive} else self.clean(v)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        return value

    def event(self, kind, **values):
        with self.lock:
            self.sequence += 1
            row = self.clean({"event": self.sequence, "kind": kind, "time": time.time(),
                              **CONTEXT.get(), **values})
            with (self.out / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=serial) + "\n")
        return self.sequence

    def save(self, relative, value):
        write_json(self.out / relative, self.clean(value))

    def snapshot(self, store, label):
        # All primary/virtual-table rows are exported. SQLite FTS shadow tables
        # remain available in the actual sqlite files rather than duplicated here.
        schema = store.db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        tables = {}
        for row in schema:
            name = row["name"]
            if name.startswith("sqlite_") or any(name.startswith(prefix + "_") for prefix in
                                                  ("fts_facts", "fts_episodes", "fts_chunks")):
                continue
            tables[name] = {"schema": row["sql"], "rows": [dict(r) for r in
                store.db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()]}
        value = {"counts": store.counts(), "pending_extract_rounds": store.backlog_count(), "tables": tables}
        self.save("states/" + label + ".json", value)
        return value["counts"]

    def install(self):
        from memsys.llm import OpenAICompatClient
        from memsys.protocols import ResponsesClient, AnthropicMessagesClient
        from memsys.core import MemorySystem
        recorder = self

        def observe_method(original, kind):
            def observed(client, values, **kwargs):
                started = time.time()
                call_id = recorder.event("model_call_start", purpose=kwargs.get("purpose"), method=kind,
                                         model=client.config.model)
                token = CONTEXT.set({**CONTEXT.get(), "call_id": call_id,
                                     "purpose": kwargs.get("purpose"), "model": client.config.model})
                row = {"call_id": call_id, "method": kind, "context": CONTEXT.get(),
                       "started_at": started, "input": list(values), "parameters": kwargs}
                recorder.save(f"calls/{call_id:04d}.json", row)
                try:
                    result = original(client, values, **kwargs)
                    row.update(success=True, output=dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result)
                    return result
                except Exception as exc:
                    row.update(success=False, error=repr(exc), traceback=traceback.format_exc())
                    raise
                finally:
                    row["elapsed_s"] = round(time.time() - started, 6)
                    recorder.save(f"calls/{call_id:04d}.json", row)
                    recorder.event("model_call_end", success=row.get("success"), elapsed_s=row["elapsed_s"],
                                   file=f"calls/{call_id:04d}.json")
                    CONTEXT.reset(token)
            return observed

        for cls in (OpenAICompatClient, ResponsesClient, AnthropicMessagesClient):
            for method in ("chat", "embed"):
                if method in cls.__dict__:
                    setattr(cls, method, observe_method(cls.__dict__[method], method))

        original_urlopen = urllib.request.urlopen

        class TeeResponse:
            def __init__(self, response, row, relative):
                self.response, self.row, self.relative = response, row, relative
                self.chunks = []
                self.saved = False

            def __getattr__(self, name):
                return getattr(self.response, name)

            def __enter__(self):
                self.response.__enter__()
                return self

            def read(self, *args, **kwargs):
                value = self.response.read(*args, **kwargs)
                self.chunks.append(value)
                return value

            def read1(self, *args, **kwargs):
                value = self.response.read1(*args, **kwargs)
                self.chunks.append(value)
                return value

            def save(self, error=None):
                if self.saved:
                    return
                self.saved = True
                raw = b"".join(self.chunks)
                text = raw.decode("utf-8", "replace")
                try:
                    body = json.loads(text)
                except ValueError:
                    body = text
                response = {"status": getattr(self.response, "status", None),
                            "headers": {k: v for k, v in self.response.headers.items()
                                        if k.lower() in {"content-type", "content-length", "date", "retry-after", "x-request-id"}},
                            "body": body, "raw_utf8": text, "captured_bytes": len(raw),
                            "scope": "Bytes actually consumed by the memory-system HTTP client"}
                self.row.update(response=response, elapsed_s=round(time.time() - self.row["started_at"], 6))
                if error:
                    self.row["error"] = repr(error)
                recorder.save(self.relative, self.row)
                recorder.event("http_attempt_end", http_id=self.row["http_id"],
                               status=response["status"], file=self.relative)

            def __exit__(self, typ, value, tb):
                try:
                    return self.response.__exit__(typ, value, tb)
                finally:
                    self.save(value)

        def observed_urlopen(request, *args, **kwargs):
            if not isinstance(request, urllib.request.Request):
                return original_urlopen(request, *args, **kwargs)
            url = urllib.parse.urlsplit(request.full_url)
            clean_url = urllib.parse.urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
            headers = dict(request.header_items())
            for key, value in headers.items():
                if key.lower() in ("authorization", "x-api-key"):
                    recorder.secrets.add(value)
                    if value.startswith("Bearer "):
                        recorder.secrets.add(value[7:])
            payload = (request.data or b"").decode("utf-8", "replace")
            try:
                body = json.loads(payload)
            except ValueError:
                body = payload
            number = recorder.event("http_attempt_start", url=clean_url)
            relative = f"http/{number:04d}.json"
            row = {"http_id": number, "context": CONTEXT.get(), "started_at": time.time(),
                   "request": {"method": request.get_method(), "url": clean_url,
                               "headers": headers, "body": body, "raw_utf8": payload,
                               "timeout_s": kwargs.get("timeout", args[1] if len(args) > 1 else None)}}
            recorder.save(relative, row)
            try:
                response = original_urlopen(request, *args, **kwargs)
            except urllib.error.HTTPError as exc:
                # Observe only bytes the ordinary error handler would read;
                # do not introduce a second read or change its retry policy.
                row.update(error=repr(exc), status=exc.code)
                consumed = []
                for method in ("read", "read1"):
                    if hasattr(exc, method):
                        original = getattr(exc, method)
                        def read_error(*a, _original=original, **kw):
                            value = _original(*a, **kw)
                            consumed.append(value)
                            row["error_body_consumed_utf8"] = b"".join(consumed).decode("utf-8", "replace")
                            recorder.save(relative, row)
                            return value
                        setattr(exc, method, read_error)
                recorder.save(relative, row)
                recorder.event("http_attempt_error", http_id=number, status=exc.code, file=relative)
                raise
            except Exception as exc:
                row.update(error=repr(exc), elapsed_s=round(time.time() - row["started_at"], 6))
                recorder.save(relative, row)
                recorder.event("http_attempt_error", http_id=number, error=repr(exc), file=relative)
                raise
            return TeeResponse(response, row, relative)

        urllib.request.urlopen = observed_urlopen
        original_close = MemorySystem.close_session
        session_number = 0

        def observed_close(system, handle, at, tags=None):
            nonlocal session_number
            session_number += 1
            store = system.store(handle.user_id)
            external = dict(store.session(handle.session_id)).get("external_id")
            token = CONTEXT.set({**CONTEXT.get(), "session_number": session_number,
                                 "session_id": handle.session_id, "external_id": external})
            prefix = f"session-{session_number:02d}"
            before = recorder.snapshot(store, prefix + "-before-close")
            recorder.event("session_close_start", before=before)
            try:
                write_report, lifecycle = original_close(system, handle, at, tags=tags)
                after = recorder.snapshot(store, prefix + "-after-close")
                row = {"session_number": session_number, "external_id": external,
                       "at": at, "before": before, "after": after,
                       "write_report": dataclasses.asdict(write_report), "lifecycle": dataclasses.asdict(lifecycle)}
                recorder.save("states/" + prefix + "-report.json", row)
                recorder.event("session_close_end", after=after, file="states/" + prefix + "-report.json")
                print(json.dumps({"stage": "session_written", "session": session_number,
                                  "external_id": external, "counts": after,
                                  "resolve_errors": write_report.resolve_errors}, ensure_ascii=False), flush=True)
                return write_report, lifecycle
            finally:
                CONTEXT.reset(token)

        MemorySystem.close_session = observed_close
        original_respond = MemorySystem.respond

        def observed_respond(system, user_id, message, at, *args, **kwargs):
            store = system.store(user_id)
            recorder.snapshot(store, "pre-answer")
            seed = recorder.out / "pre-answer.sqlite"
            with sqlite3.connect(store.path) as source, sqlite3.connect(seed) as target:
                source.backup(target)
            recorder.event("answer_stage_start", question=message, at=at, pre_answer_sha256=sha(seed))
            result = original_respond(system, user_id, message, at, *args, **kwargs)
            recorder.save("reply.json", {"text": result.text, "trace": result.trace})
            recorder.snapshot(store, "post-answer")
            recorder.event("answer_stage_end", answer=result.text)
            return result

        MemorySystem.respond = observed_respond


def run(args):
    freeze = read_json(args.out / "freeze.json")
    if sha(__file__) != freeze["observer_sha256"]:
        raise ValueError("Observer changed after freezing")
    if (args.out / "status.json").exists():
        raise ValueError("This attempt has already started; do not rerun to change its answer")
    for relative, expected in freeze["source_files"].items():
        if sha(args.out / "frozen-src" / relative) != expected:
            raise ValueError("Frozen source changed: " + relative)
    if sha(args.out / "config.frozen.json") != freeze["config_sha256"] or sha(args.out / "source-case.no-label.json") != freeze["case_sha256"]:
        raise ValueError("Frozen configuration or label-free input changed")
    sys.path.insert(0, str(args.out / "frozen-src"))
    from memsys.bench.longmemeval import load, _split
    from memsys.bench.runner import Runner
    from memsys.bench.judge import judge
    from memsys.cli import load_dotenv
    from memsys.config import AppConfig
    from memsys.llm import UsageLedger
    load_dotenv(str(ROOT / ".env"))
    recorder = Recorder(args.out)
    recorder.install()
    started = time.time()
    status = {"status": "running", "question_id": freeze["question_id"], "started_at": started}
    recorder.save("status.json", status)
    try:
        config = AppConfig.load(str(args.out / "config.frozen.json"))
        ledger = UsageLedger(str(args.out / "usage.jsonl"))
        models = config.build_models(ledger)
        item, _empty_gold = load(str(args.out / "source-case.no-label.json"))[0]
        if _empty_gold.answer:
            raise AssertionError("Reference answer entered the replay input")
        runner = Runner(models, config.memory, str(args.out / "run"), progress=print,
                        data_info={"sha256": freeze["case_sha256"], "file": "source-case.no-label.json"},
                        max_wall_s=args.max_wall_s)
        counts = runner.run([item], workers=1)
        status.update(counts=counts, runner_aborted=runner.aborted)
        if counts["answered"] != 1:
            raise RuntimeError("The single normal replay did not finish; retain errors, do not regenerate")
        # Label access starts only after the normal system has sealed its answer.
        recorder.event("reference_label_access_start", sealed_answer_sha256=sha(args.out / "run" / "hypotheses.jsonl"))
        data = Path(freeze["source_data"])
        if sha(data) != freeze["source_data_sha256"]:
            raise ValueError("Original label file changed since selection")
        raw = next(row for row in read_json(data) if row["question_id"] == item.question_id)
        _, gold = _split(raw)
        recorder.save("reference-answer.judge-only.json", gold)
        evaluation = judge(str(args.out / "run" / "hypotheses.jsonl"), {gold.question_id: gold},
                           config.client("judge", ledger), config.roles["judge"], workers=1, progress=print)
        labels = [json.loads(line) for line in Path(evaluation).read_text().splitlines() if line.strip()]
        status.update(status="completed", evaluation_file=evaluation,
                      correct=labels[0]["autoeval_label"]["label"], totals=ledger.totals(), usage_by_purpose=ledger.summary(),
                      cost_known=all(r.cost_known is True for r in ledger.records()),
                      pricing_note="Unknown provider prices are not zero cost; raw ledger cost remains a compatibility field",
                      observer_overhead="Snapshot and full I/O capture add overhead; timings are not production performance")
    except Exception as exc:
        status.update(status="failed", error=repr(exc), traceback=traceback.format_exc())
        recorder.event("run_error", error=repr(exc))
        raise
    finally:
        status["wall_s"] = round(time.time() - started, 3)
        recorder.save("status.json", status)
        render(args.out)
    print(json.dumps(status, ensure_ascii=False), flush=True)


def render(out):
    esc = html.escape
    def block(title, value, opened=False):
        body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=serial)
        return '<details' + (' open' if opened else '') + '><summary>' + esc(title) + '</summary><pre>' + esc(body) + '</pre></details>'
    input_data = read_json(out / "input.json")
    status = read_json(out / "status.json") if (out / "status.json").exists() else {"status": "prepared"}
    pieces = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>跨会话单题完整追踪</title>',
              '<style>body{font:15px/1.65 system-ui,sans-serif;max-width:1140px;margin:36px auto;padding:0 22px;background:#fafafa;color:#202428}h1,h2{line-height:1.3}details{background:white;border:1px solid #d8dde3;border-radius:7px;margin:12px 0;padding:10px 14px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.65 ui-monospace,monospace}a{color:#175abd}.notice{padding:14px;background:#fff0cd}nav a{margin-right:16px}</style>',
              '<h1>跨会话单题完整追踪 · ' + esc(input_data["question_id"]) + '</h1>',
              '<p class="notice">仅运行自研 memsys；独立空库、冻结当前配置、不修改规则、不回放模型响应。此题来自已用过的开发数据，不是盲测；评分不能证明泛化。认证信息已隐藏。这里记录的是模型公开返回的输出，不是未公开的内部推理。</p>',
              '<nav><a href="#source">原始输入</a><a href="#write">写入过程</a><a href="#read">检索与回答</a><a href="#models">模型输入输出</a><a href="#http">HTTP原始记录</a><a href="#judge">独立评分</a><a href="#config">配置与指纹</a></nav>',
              '<h2>问题</h2><pre>' + esc(input_data["question"]) + '</pre>', block("运行状态、实际用量与耗时", status, True),
              '<h2 id="source">1. 全部原始历史会话（按回放时间排序）</h2>']
    for number, session in enumerate(input_data["sessions"], 1):
        text = f'日期：{session["date"]}\n会话：{session["session_id"]}\n'
        for idx, turn in enumerate(session["turns"], 1):
            text += f'\n[{idx:02d} {turn["role"]}]\n{turn["content"]}\n'
        pieces.append(block(f'会话 {number} · {session["date"]} · {len(session["turns"])} 条消息', text, True))
    pieces.append('<h2 id="write">2. 每个会话的写入报告、更新裁决、生命周期与数据库状态</h2>')
    for path in sorted((out / "states").glob("session-*-report.json")):
        row = read_json(path)
        pieces.append(block(path.name, row, True))
        prefix = path.name.removesuffix("-report.json")
        for suffix in ("before-close", "after-close"):
            snapshot = out / "states" / f"{prefix}-{suffix}.json"
            pieces.append(block(f'{prefix} · {suffix} · 全部主表数据', read_json(snapshot)))
    if (out / "states" / "pre-answer.json").exists():
        pieces.append(block("回答之前的全部记忆、原文、分块、向量、来源链接", read_json(out / "states" / "pre-answer.json")))
        pieces.append('<p><a href="pre-answer.sqlite">读取前数据库</a> · <a href="states/pre-answer.json">读取前完整JSON</a></p>')
    pieces.append('<h2 id="read">3. 检索路径、计划、候选、重排、证据预算与最终回答</h2>')
    if (out / "reply.json").exists():
        reply = read_json(out / "reply.json")
        pieces.append(block("系统最终回答（原文）", reply["text"], True))
        pieces.append(block("完整检索与验证 trace（不省略字段）", reply["trace"], True))
    pieces.append('<h2 id="models">4. 所有模型调用：实际输入与输出（含抽取、裁决、查询、分页、复核、评分、embedding）</h2>')
    for path in sorted((out / "calls").glob("*.json")):
        row = read_json(path)
        context = row["context"]
        purpose = context.get("purpose", "unknown")
        title = f'调用 {row["call_id"]} · {purpose} · {context.get("model")} · {row.get("elapsed_s", "运行中")}秒'
        pieces.append(block(title, row, row["method"] == "chat"))
    pieces.append('<h2 id="http">5. 每次实际 HTTP 尝试（含重试；原始请求体、响应体与实际读到的字节）</h2>')
    for path in sorted((out / "http").glob("*.json")):
        row = read_json(path)
        pieces.append(block(f'HTTP {row["http_id"]} · {row["context"].get("purpose", "初始化")} · {row["request"]["url"]}', row))
    pieces.append('<h2 id="judge">6. 仅在最终回答完成后读取的参考答案与评分</h2>')
    if (out / "reference-answer.judge-only.json").exists():
        pieces.append(block("评分专用参考标签（未传入记忆系统）", read_json(out / "reference-answer.judge-only.json"), True))
    if status.get("evaluation_file"):
        pieces.append(block("原始评分结果", Path(status["evaluation_file"]).read_text(), True))
    pieces.append('<h2 id="config">7. 冻结配置、代码指纹及完整事件时间线</h2>')
    for name in ("freeze.json", "config.frozen.json", "events.jsonl", "usage.jsonl"):
        path = out / name
        if path.exists():
            pieces.append(block(name, path.read_text()))
            pieces.append('<p><a href="' + esc(name) + '">' + esc(name) + '</a></p>')
    pieces.append('</html>')
    (out / "report.html").write_text("\n".join(pieces))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("prepare", "run", "report"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "longmemeval_oracle.json")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--max-wall-s", type=float, default=540)
    args = parser.parse_args()
    args.out = args.out.resolve()
    if args.stage == "prepare":
        prepare(args)
    elif args.stage == "run":
        run(args)
    else:
        render(args.out)


if __name__ == "__main__":
    main()
