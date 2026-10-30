"""Qdrant maintenance plans. Physical compaction requires stopped readers and writers."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from .schema import ARCHIVED, EPISODE_WITHHELD, FACT_CURRENT, FORGOTTEN


def _seal(plan):
    plan["sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


def _verify(plan):
    if _seal({k: v for k, v in plan.items() if k != "sha256"})["sha256"] != plan.get("sha256"):
        raise ValueError("maintenance plan changed; create a fresh preview")


def repair_plan(store, sessions=(), failure_log=None):
    requested = set(sessions)
    if failure_log:
        content = Path(failure_log).read_text()
        try:
            rows = json.loads(content)
            rows = rows if isinstance(rows, list) else [rows]
        except ValueError:
            rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        requested.update(str(r["session_id"]) for r in rows if isinstance(r, dict) and r.get("session_id") and
                         (r.get("extract_failures") or r.get("retry_rounds")))
    with store.read_snapshot():
        selected, skipped = [], []
        for name in requested:
            matches = [r for r in store.records("session") if r["id"] == name or r.get("external_id") == name]
            if len(matches) != 1 or matches[0]["status"] != "closed":
                raise ValueError("repair requires one closed session: " + name)
            sid = matches[0]["id"]
            for row in store.records("episode", session_id=sid):
                repair = store.record("repair_request", row["id"])
                if row["status"] in (FORGOTTEN, EPISODE_WITHHELD, "skipped") or row.get("safe_view") or repair and repair.get("status") == "completed":
                    skipped.append(row["id"])
                else:
                    selected.append(dict(episode_id=row["id"], session_id=sid, reason="explicit session selection"))
        return _seal(dict(kind="legacy-repair", revision=store.get_meta("revision"), tenant=store.tenant,
                          episodes=selected, skipped=skipped))


def queue_repair(store, plan):
    _verify(plan)
    with store.transaction():
        if plan.get("kind") != "legacy-repair" or plan.get("tenant") != store.tenant or plan["revision"] != store.get_meta("revision"):
            raise ValueError("repair preview is stale or belongs to another tenant")
        for row in plan["episodes"]:
            episode = store.record("episode", row["episode_id"])
            if not episode or episode["status"] in (FORGOTTEN, EPISODE_WITHHELD) or episode.get("safe_view"):
                raise ValueError("cannot repair forgotten or redacted sources")
            store.add_backlog([row["episode_id"]])
            store.put_record("repair_request", row["episode_id"], dict(episode_id=row["episode_id"], reason=row["reason"], status="queued"))
    return len(plan["episodes"])


def cleanup_plan(store, archive_before=None):
    cutoff = datetime.fromisoformat(archive_before) if archive_before else None
    with store.read_snapshot():
        facts = [f.id for f in store.facts() if f.status == FORGOTTEN or cutoff and f.status == ARCHIVED and f.recorded_at < cutoff]
        sources = {eid for values in store.sources(facts).values() for eid in values}
        actions = []
        for episode in store.episodes():
            related = store.facts_of_episodes([episode.id])[episode.id]
            live = [f.id for f in store.facts(related) if f.id not in facts and f.status != FORGOTTEN]
            eligible = cutoff and episode.status == ARCHIVED and episode.at < cutoff and not any(f.status == FACT_CURRENT for f in store.facts(live))
            if episode.id in sources or episode.status in (FORGOTTEN, EPISODE_WITHHELD) or eligible:
                raw = store.record("episode", episode.id)
                mode = "replace_with_safe_view" if raw.get("safe_view") else "withheld_tombstone" if live else "delete"
                actions.append(dict(episode_id=episode.id, session_id=episode.session_id, mode=mode))
        return _seal(dict(kind="cleanup", tenant=store.tenant, revision=store.get_meta("revision"), facts=facts,
                          episodes=actions, archive_before=archive_before,
                          turn_prefixes={a["session_id"]: store.session(a["session_id"])["turns_done"] for a in actions}))


def integrity(store):
    with store.read_snapshot():
        facts = {r["id"]: r for r in store.records("fact")}
        episodes = {r["id"]: r for r in store.records("episode")}
        sessions = {r["id"]: r for r in store.records("session")}
        return dict(sources=all(eid in episodes for f in facts.values() for eid in f.get("source_ids", [])),
                    versions=all(not f.get("superseded_by") or f["superseded_by"] in facts for f in facts.values()),
                    chunks=all(c["episode_id"] in episodes for c in store.records("episode_chunk")),
                    backlog=all(r["episode_id"] in episodes for r in store.records("backlog")),
                    sessions=all(r["session_id"] in sessions for r in store.records("turn")),
                    spans=all(s["episode_id"] in episodes for f in facts.values() for s in f.get("spans", [])),
                    semantics=all(not f.get("semantics") for f in facts.values() if f["status"] == FORGOTTEN))


def apply_cleanup(store, plan, backup_path, *, offline=False):
    if not offline:
        raise ValueError("physical cleanup requires --offline and all clients stopped")
    _verify(plan)
    if plan.get("kind") != "cleanup" or plan.get("tenant") != store.tenant:
        raise ValueError("wrong cleanup plan")
    store.backup(backup_path)
    before = store.allocated_bytes()
    with store.transaction():
        if cleanup_plan(store, plan.get("archive_before"))["sha256"] != plan["sha256"]:
            raise ValueError("cleanup preview is stale")
        for row in store.records("index_generation"):
            store.delete_record("index_generation", row["id"])
        for key in ("active_index_generation", "previous_index_generation", "index_source_sha256"):
            store.delete_record("meta", key)
        for fid in plan["facts"]:
            row = store.record("fact", fid)
            row.update(text="", subject="", attribute="", value="", event_start=None, event_end=None,
                       precision=None, confidence=0, importance=0, status=FORGOTTEN, source_ids=[], spans=[], semantics={})
            row.pop("vector", None)
            store.put_record("fact", fid, row)
        for action in plan["episodes"]:
            eid = action["episode_id"]
            row = store.record("episode", eid)
            store.put_episode_chunks(eid, [])
            store.clear_backlog([eid])
            store.delete_record("repair_request", eid)
            row.pop("vector", None)
            safe = row.pop("safe_view", None)
            if action["mode"] == "replace_with_safe_view":
                row.update(safe)
            else:
                row.update(user_text="[purged]", assistant_text="", status=EPISODE_WITHHELD if action["mode"] == "withheld_tombstone" else FORGOTTEN)
            store.put_record("episode", eid, row)
            for fact in store.records("fact"):
                if any(s["episode_id"] == eid for s in fact.get("spans", [])):
                    fact["spans"] = [s for s in fact["spans"] if s["episode_id"] != eid]
                    store.put_record("fact", fact["id"], fact)
        for sid, end in plan["turn_prefixes"].items():
            for turn in store.turns(sid):
                if turn["idx"] < end:
                    turn["content"] = turn.pop("safe_content", "[purged audit]")
                    store.put_record("turn", sid + ":" + str(turn["idx"]), turn)
        if not all(integrity(store).values()):
            raise ValueError("cleanup violated record integrity")
    compacted = store.compact(offline=True)
    return dict(before_bytes=before, after_bytes=store.allocated_bytes(), backup=str(backup_path),
                facts_cleared=len(plan["facts"]), episodes_processed=len(plan["episodes"]),
                compacted=compacted, integrity=integrity(store), cap_met=store.max_bytes is None or store.allocated_bytes() <= store.max_bytes)
