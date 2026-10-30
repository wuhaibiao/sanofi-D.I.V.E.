#!/usr/bin/env python3
"""Isolated web-memory acceptance with real providers and two independent processes.

Authored regression, not an accuracy benchmark. Only source turns and questions
reach product models. Diagnostic conversations never enter the local account.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from memsys.bench.provenance import model_manifest, source_manifest
from memsys.config import AppConfig
from memsys.llm import UsageLedger
from memsys.qdrant import Qdrant
from memsys.serve import MemoryService
from memsys.webchat import WebChat


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def inputs(args):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [Path(__file__).resolve(), args.suite.resolve(), args.config.resolve(),
                         *sorted((ROOT / "src/memsys/web").glob("*"))] if path.is_file()}


def evaluate(answer, query):
    """Grade after generation; expected values never enter a product prompt."""
    failures = []
    for text in query.get("contains", []):
        if text not in answer:
            failures.append("missing: " + text)
    for text in query.get("forbidden", []):
        if text in answer:
            failures.append("forbidden: " + text)
    for pattern in query.get("forbidden_regex", []):
        if re.search(pattern, answer):
            failures.append("unsupported detail: " + pattern)
    if "number" in query and re.findall(r"\d+", answer) != [str(query["number"])]:
        failures.append("incorrect or ambiguous number")
    if query.get("unknown") and not re.search(
            r"不知道|不清楚|不记得|没有.*(?:信息|记录|日期)|未.*(?:提供|提到|记录)|无法确定|unknown", answer, re.I):
        failures.append("did not acknowledge missing information")
    return dict(passed=not failures, failures=failures)


def credentials(runtime_path):
    state = json.loads(runtime_path.read_text())
    if Path(state["project_root"]) != ROOT:
        raise ValueError("runtime belongs to a different project")
    spec = importlib.util.spec_from_file_location("local_deploy", ROOT / "deploy/local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in module.dotenv(ROOT / ".env").items():
        os.environ.setdefault(key, value)
    os.environ["QDRANT_API_KEY"] = state["tokens"]["qdrant"]
    os.environ["EMBEDDING_API_KEY"] = state["tokens"]["embedding"]
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


class CapturedChat:
    def __init__(self, client, path):
        self.client, self.path = client, path
        self.config = getattr(client, "config", None)

    def chat(self, messages, **kwargs):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(purpose=kwargs["purpose"], messages=messages,
                tags=kwargs.get("tags", {})), ensure_ascii=False) + "\n")
        return self.client.chat(messages, **kwargs)


def phase(args):
    checkpoint = json.loads((args.out / "checkpoint.json").read_text())
    if source_manifest()["source_sha256"] != checkpoint["source"]["source_sha256"]:
        raise ValueError("source changed; use a fresh output directory")
    if inputs(args) != checkpoint["inputs"]:
        raise ValueError("suite, runner, configuration or UI changed; use a fresh output directory")
    fixture = json.loads(args.suite.read_text())
    credentials(args.runtime)
    config = AppConfig.load(args.config)
    storage = replace(config.storage, collection_prefix=checkpoint["prefix"], namespace=checkpoint["namespace"])
    ledger = UsageLedger(str(args.out / (args.phase + "-usage.jsonl")))
    models = config.build_models(ledger, env={}, probe=False)
    models.chat = CapturedChat(models.chat or models.memory, args.out / (args.phase + "-prompts.jsonl"))
    models.by_purpose["extract"] = CapturedChat(models.for_purpose("extract"), args.out / (args.phase + "-prompts.jsonl"))
    clock = [datetime.fromisoformat(fixture["today"])]
    service = MemoryService(models, config.memory, str(args.out / args.phase), storage=storage,
        clock=lambda: clock[0], tick_s=3600, log=lambda text: print(text, flush=True))
    web = WebChat(service)
    checks, results = [], []
    started = time.monotonic()
    try:
        for case in fixture["cases"]:
            user = "acceptance-" + case["id"]
            store = service.req.store(user)
            print(args.phase + ": " + case["id"], flush=True)
            if args.phase == "write":
                sources = []
                for index, session in enumerate(case["sessions"]):
                    clock[0] = datetime.fromisoformat(session["at"])
                    key = web.create(user)["id"]
                    sid = store.record("web_conversation", key)["session_ids"][-1]
                    turns = session["turns"] + [turn for n in range(session.get("filler_rounds", 0))
                        for turn in [dict(role="user", content="这是一轮普通闲聊 %d。" % n),
                                     dict(role="assistant", content="收到。")]]
                    for turn in turns:
                        store.append_turn(sid, turn["role"], turn["content"], clock[0])
                    sources.append(dict(conversation_id=key, session_id=sid, raw_messages=len(turns)))
                    if not case.get("pending"):
                        web.change(user, key, close=True)
                        if not service.drain(timeout=180):
                            raise TimeoutError("background extraction exceeded 180 seconds")
                    row = store.session(sid)
                    valid = (row["status"] == "open" and row["turns_done"] == 0 if case.get("pending")
                             else row["status"] == "closed" and row["turns_done"] == len(turns))
                    checks.append(dict(check=case["id"] + "/durable-source-" + str(index), passed=bool(valid),
                        status=row["status"], turns_done=row["turns_done"], raw_messages=len(store.turns(sid))))
                facts = store.facts()
                if "max_facts" in case:
                    checks.append(dict(check=case["id"] + "/memory-selection",
                        passed=len(facts) <= case["max_facts"], actual=[f.text for f in facts]))
                if case.get("forget_contains"):
                    targets = [f.key for f in facts if case["forget_contains"] in f.text]
                    checks.append(dict(check=case["id"] + "/forget-target-found", passed=bool(targets)))
                    for key in targets:
                        service.forget(user, key)
                checkpoint["sources"][case["id"]] = sources
                save(args.out / "checkpoint.json", checkpoint)
                results.append(dict(case=case["id"], sources=sources,
                    facts=[dict(key=f.key, text=f.text, kind=f.kind, status=f.status) for f in store.facts()],
                    jobs=[{k: j.get(k) for k in ("status", "attempts", "queued_at", "started_at", "completed_at", "error")}
                          for j in store.records("job")]))
            else:
                clock[0] = datetime.fromisoformat(fixture["today"])
                for index, query in enumerate(case["queries"]):
                    key = (checkpoint["sources"][case["id"]][0]["conversation_id"] if query.get("same_conversation")
                           else web.create(user)["id"])
                    begin = time.monotonic()
                    reply = web.send(user, key, query["question"], "acceptance-" + case["id"] + "-" + str(index))
                    judgment = evaluate(reply["content"], query)
                    results.append(dict(case=case["id"], query=query["id"], question=query["question"],
                        answer=reply["content"], memory_items=reply["memory_items"],
                        latency_s=round(time.monotonic()-begin, 3), **judgment))
                    checks.append(dict(check=case["id"] + "/" + query["id"], **judgment))
                    if not query.get("unknown"):
                        checks.append(dict(check=case["id"] + "/" + query["id"] + "/evidence-present",
                                           passed=bool(reply["memory_items"])))
                    print("  " + query["id"] + ": " + ("PASS" if judgment["passed"] else "FAIL")
                          + " · " + reply["content"], flush=True)
    finally:
        service.shutdown(timeout=3)
        save(args.out / (args.phase + ".json"), dict(phase=args.phase, independent_process_pid=os.getpid(),
            elapsed_s=round(time.monotonic()-started, 3), source=source_manifest(), models=model_manifest(models),
            checks=checks, results=results, usage=ledger.summary(),
            pricing_known=all(r.cost_known for r in ledger.records()),
            status="passed" if checks and all(row["passed"] for row in checks) else "failed"))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=Path.home() / "Library/Application Support/memsys/runtime.json")
    parser.add_argument("--config", type=Path, default=Path.home() / "Library/Application Support/memsys/config.json")
    parser.add_argument("--suite", type=Path, default=ROOT / "tests/fixtures/web_memory_reliability.json")
    parser.add_argument("--phase", choices=("write", "read"))
    args = parser.parse_args()
    args.out = args.out.resolve()
    if args.phase:
        return phase(args)
    if args.out.exists():
        raise ValueError("use a fresh output directory to preserve earlier evidence")
    args.out.mkdir(parents=True)
    prefix = "memsys_accept_" + uuid.uuid4().hex[:16]
    checkpoint = dict(prefix=prefix, namespace="web-memory-acceptance", source=source_manifest(), inputs=inputs(args), sources={})
    save(args.out / "checkpoint.json", checkpoint)
    outcomes, removed = [], []
    begin = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        for name in ("write", "read"):
            command = [sys.executable, str(Path(__file__).resolve()), "--phase", name, "--out", str(args.out),
                       "--runtime", str(args.runtime), "--config", str(args.config), "--suite", str(args.suite)]
            with (args.out / (name + ".log")).open("w") as stream:
                result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=600)
            print(name + " process exit: " + str(result.returncode), flush=True)
            outcomes.append(dict(phase=name, exit_code=result.returncode))
            if result.returncode:
                break
    finally:
        credentials(args.runtime)
        config = AppConfig.load(args.config)
        client = Qdrant(config.storage)
        for collection in client.request("GET", "/collections")["collections"]:
            if collection["name"].startswith(prefix + "_"):
                client.request("DELETE", "/collections/" + collection["name"])
                removed.append(collection["name"])
        phases = [json.loads((args.out / (name + ".json")).read_text())
                  for name in ("write", "read") if (args.out / (name + ".json")).exists()]
        checks = [row for report in phases for row in report["checks"]]
        passed = (len(phases) == 2 and all(row["exit_code"] == 0 for row in outcomes)
                  and all(row["passed"] for row in checks))
        report = dict(status="passed" if passed else "failed", mode="authored regression, not accuracy benchmark",
            started_at=started_at, elapsed_s=round(time.monotonic()-begin, 3),
            source=checkpoint["source"], suite_sha256=hashlib.sha256(args.suite.read_bytes()).hexdigest(),
            inputs=checkpoint["inputs"],
            config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),
            process_results=outcomes, independent_processes=[r["independent_process_pid"] for r in phases],
            checks_passed=sum(row["passed"] for row in checks), checks_total=len(checks), checks=checks,
            temporary_collections_removed=removed,
            limitations=["Small authored scenarios cannot establish long-term statistical reliability.",
                         "No multi-day unattended operation or large-scale latency/load claim.",
                         "Provider prices are unconfigured; currency cost stays unknown."])
        save(args.out / "report.json", report)
        print(json.dumps({k: report[k] for k in ("status", "checks_passed", "checks_total", "elapsed_s")}), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
