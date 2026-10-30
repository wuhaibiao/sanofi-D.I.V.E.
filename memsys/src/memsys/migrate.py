"""Explicit, read-only legacy SQLite import. Never opens SQLite in the online path."""
import hashlib
import json
import sqlite3
from array import array
from pathlib import Path
from .maintenance import integrity
from .schema import FORGOTTEN, EPISODE_WITHHELD


def import_sqlite(source, target):
    source = Path(source).resolve(strict=True)
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def rows(table):
        return [dict(r) for r in conn.execute('SELECT * FROM "' + table + '"')] if table in tables else []
    try:
        sessions, turns, episodes, facts = (rows(t) for t in ("sessions", "turns", "episodes", "facts"))
        semantics = {r["fact_id"]: json.loads(r["payload"]) for r in rows("fact_semantics")}
        sources, spans = {}, {}
        for r in rows("fact_sources"):
            sources.setdefault(r["fact_id"], []).append(r["episode_id"])
        for r in rows("fact_spans"):
            spans.setdefault(r.pop("fact_id"), []).append(r)
        views = {r["episode_id"]: {k: r[k] for k in ("user_text", "assistant_text")} for r in rows("episode_views")}
        turn_views = {(r["session_id"], r["idx"]): r["content"] for r in rows("turn_views")}
        chunks, vectors = rows("episode_chunks"), rows("vectors")
        if any(r.get("parent_id") is not None for r in chunks):
            raise ValueError("experimental parent chunk index requires explicit reindex; import flat legacy indexes only")
        vector_map = {}
        for r in vectors:
            vec = array("f")
            vec.frombytes(r["vec"])
            vector_map[(r["kind"], r["item_id"])] = list(vec)
        with target.transaction():
            if any(target.records(kind) for kind in ("session", "fact", "episode", "meta")):
                raise ValueError("migration target tenant must be empty; use a new namespace or user")
            for r in sessions:
                r["next_idx"] = max((t["idx"] + 1 for t in turns if t["session_id"] == r["id"]), default=0)
                target.put_record("session", r["id"], r)
            for r in turns:
                if (r["session_id"], r["idx"]) in turn_views:
                    r["safe_content"] = turn_views[(r["session_id"], r["idx"])]
                target.put_record("turn", r["session_id"] + ":" + str(r["idx"]), r)
            for r in episodes:
                if r["id"] in views:
                    r["safe_view"] = views[r["id"]]
                if ("episode", r["id"]) in vector_map:
                    r["vector"] = vector_map[("episode", r["id"])]
                target.put_record("episode", r["id"], r)
            by_episode = {r["id"]: r for r in episodes}
            for r in chunks:
                parent = by_episode.get(r["episode_id"])
                if not parent:
                    raise ValueError("legacy chunk has no source")
                original = (parent.get("safe_view") or parent)["user_text" if r["role"] == "User" else "assistant_text"]
                r["text"] = r["role"] + ": " + original[r["start"]:r["end"]]
                r["status"] = parent["status"]
                if ("episode_chunk", r["id"]) in vector_map:
                    r["vector"] = vector_map[("episode_chunk", r["id"])]
                target.put_record("episode_chunk", r["id"], r)
            for r in facts:
                r.update(source_ids=sorted(sources.get(r["id"], [])), spans=spans.get(r["id"], []),
                         semantics={} if r["status"] == FORGOTTEN else semantics.get(r["id"], {}))
                if ("fact", r["id"]) in vector_map:
                    r["vector"] = vector_map[("fact", r["id"])]
                target.put_record("fact", r["id"], r)
            for table, kind, key in (("pending", "pending", "id"), ("extract_backlog", "backlog", "episode_id"),
                                     ("repair_requests", "repair_request", "episode_id"),
                                     ("conversation_exclusions", "conversation_exclusion", "session_id")):
                for r in rows(table):
                    target.put_record(kind, r[key], r)
            for r in rows("meta"):
                if r["key"] not in ("revision", "active_index_generation", "previous_index_generation", "index_source_sha256"):
                    target.set_meta(r["key"], r["value"])
            snapshot = dict(sessions=sessions, turns=turns, facts=facts, episodes=episodes, chunks=chunks)
            digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
            target.set_meta("migration_source_sha256", digest)
            if not all(integrity(target).values()):
                raise ValueError("legacy source references are inconsistent")
            target.validate_index()
        return dict(source=str(source), source_read_only=True, snapshot_sha256=digest, user=target.user_id,
                    counts={k: len(v) for k, v in snapshot.items()},
                    index_generations="not imported; originals and current vectors retained")
    finally:
        conn.rollback()
        conn.close()
