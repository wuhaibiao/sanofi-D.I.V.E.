"""持续写入、更新、归档、遗忘、补偿、重启与物理维护的合成运行验收。"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from memsys.bench.provenance import source_manifest
from memsys.config import MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.maintenance import apply_cleanup, cleanup_plan, integrity, queue_repair, repair_plan


def measure(cycles=600):
    def extract(messages):
        text = messages[-1]["content"]
        match = re.search(r"Counter value: (\d+)\. Artifact: (artifact\d+)\.", text)
        if not match:
            marker = re.search(r"Legacy marker: (legacy\d+)\.", text)
            if not marker:
                raise ValueError("unexpected synthetic source")
            value = marker.group(1)
            return json.dumps({"facts": [{"text": "The user keeps " + value + ".", "attribute": value,
                "value": value, "rounds": [1], "importance": 2}]})
        value, artifact = match.groups()
        return json.dumps({"facts": [
            {"text": "The user's current counter value is " + value + ".", "attribute": "counter", "value": value,
             "rounds": [1], "importance": 5, "source_quotes": [{"round": 1, "role": "user", "quote": "Counter value: " + value + "."}]},
            {"text": "The user owns " + artifact + ".", "attribute": artifact, "value": artifact,
             "rounds": [1], "importance": 2, "source_quotes": [{"round": 1, "role": "user", "quote": "Artifact: " + artifact + "."}]}]})
    def resolve(messages):
        decisions = []
        for block in re.split(r"(?m)(?=^\[N\d+\])", messages[-1]["content"]):
            new = re.match(r"\[(N\d+)\] ([^\n]+)", block)
            old = re.search(r"\[(F\d+)\]", block)
            if new:
                decisions.append({"new": new.group(1), "action": "update" if "counter value" in new.group(2) and old else "new",
                                  "target": old.group(1) if old else None})
        return json.dumps({"decisions": decisions})
    chat = ScriptedChat({"extract": extract, "resolve": resolve})
    embed = HashEmbedder(dim=64)
    models = Models(None, chat, embed, "hash-64")
    settings = MemorySettings(query_analysis=False, rerank_backend="fusion", resolve_similarity=.99,
                              min_similarity=.05, context_budget_tokens=1000, max_history_share=0,
                              max_active_facts=48, max_active_episodes=24)
    events = {"restarts": 0, "repairs": 0, "cleanups": 0, "reindexes": 0, "forgets": 0}
    checks = {"latest_state": True, "capacity": True, "forgotten_not_recalled": True,
              "integrity": True, "restart_idempotent": True, "strict_budget": True}
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        system = MemorySystem(models, settings, data_dir=tmp)
        try:
            for n in range(1, cycles + 1):
                at = datetime(2023, 1, 1, 10) + timedelta(days=n)
                artifact = "artifact%05d" % n
                handle = system.open_session("u", at)
                system.add_turn(handle, "user", "Counter value: %d. Artifact: %s." % (n, artifact), at)
                system.close_session(handle, at)
                store = system.store("u")
                current = [f for f in system.facts("u") if f.attribute == "counter"]
                checks["latest_state"] &= len(current) == 1 and current[0].value == str(n)
                counts = store.counts()
                checks["capacity"] &= sum(counts["fact"].get(s, 0) for s in ("current", "superseded")) <= 48
                checks["capacity"] &= counts["episode"].get("active", 0) <= 24
                if n % 60 == 0:
                    target = next(f for f in system.facts("u") if f.value == artifact)
                    system.forget("u", target.key)
                    recall = system.recall("u", artifact, at)
                    checks["forgotten_not_recalled"] &= artifact not in recall.memory
                    checks["strict_budget"] &= recall.trace["memory_tokens"] <= recall.trace["effective_budget"]
                    events["forgets"] += 1
                if n % 100 == 0:
                    chat.handlers["extract"] = "malformed output"
                    legacy = system.open_session("u", at)
                    system.add_turn(legacy, "user", "Legacy marker: legacy%d." % n, at)
                    system.close_session(legacy, at)
                    with store.transaction():
                        store.clear_backlog([e.id for e in store.backlog()])
                    chat.handlers["extract"] = extract
                    queue_repair(store, repair_plan(store, [legacy.session_id]))
                    system.rebuild("u")
                    checks["restart_idempotent"] &= store.backlog_count() == 0
                    events["repairs"] += 1
                if n % 200 == 0:
                    result = apply_cleanup(store, cleanup_plan(store), str(Path(tmp) / ("backup-%d.sqlite" % n)))
                    checks["integrity"] &= all(result["integrity"].values())
                    events["cleanups"] += 1
                if n % 50 == 0:
                    checks["integrity"] &= all(integrity(store).values())
                    system.close()
                    system = MemorySystem(models, settings, data_dir=tmp)
                    checks["restart_idempotent"] &= system.recover("u") == []
                    system.reindex("u")
                    events["restarts"] += 1
                    events["reindexes"] += 1
                if n % 100 == 0:
                    print("Cycles %d/%d; restarts %d; checks %s" % (n, cycles, events["restarts"], all(checks.values())), flush=True)
            return {"mode": "synthetic continuous memory operation; scripted extraction, no answer accuracy score",
                    "cycles": cycles, "events": events, "checks": checks, "passed": all(checks.values()),
                    "seconds": round(time.monotonic() - started, 2), "settings": settings.to_dict(),
                    "storage": system.store("u").storage_usage(), "provenance": source_manifest()}
        finally:
            system.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cycles", type=int, default=600)
    args = parser.parse_args()
    if args.cycles < 200:
        raise ValueError("at least 200 cycles are required to exercise every maintenance phase")
    report = measure(args.cycles)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
