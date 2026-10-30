"""Qdrant primary storage with immutable record revisions and a publication journal.

All data, including raw messages, lives on the Qdrant server. A short conditional
head update publishes a staged operation. Readers never expose uncommitted records.
This application protocol is not a native multi-point Qdrant transaction. Concurrent
writes to one user may conflict and must be retried; different users have separate heads.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import random
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date
from functools import wraps
from pathlib import Path

from .qdrant import Qdrant, QdrantSettings, QdrantError, lexical_vector, match
from .schema import Episode, EpisodeChunk, Fact, FACT_CURRENT, FACT_SUPERSEDED, FORGOTTEN, EPISODE_WITHHELD, normalize_semantics
from .textutil import estimate_tokens
from .timeutil import DateSpan, from_iso, to_iso
from .vecmath import VectorRows


class EmbeddingMismatch(RuntimeError):
    pass


class StorageLimitExceeded(ValueError):
    pass


class WriteConflict(RuntimeError):
    """No records from this operation were published; retry from fresh state."""


def atomic(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        with self.transaction():
            return fn(self, *args, **kwargs)
    return call


def retry_write(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        for attempt in range(8):
            try:
                return fn(self, *args, **kwargs)
            except WriteConflict:
                if attempt == 7:
                    raise
                time.sleep(random.uniform(.005, .03) * (attempt + 1))
    return call


class Store:
    def __init__(self, namespace=None, *, user_id="default", settings=None, client=None,
                 max_bytes=None, wal=False):
        self.settings = settings or QdrantSettings.environment()
        if namespace is not None:
            self.settings = copy.copy(self.settings)
            self.settings.namespace = namespace
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id must be a nonempty string")
        if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0):
            raise ValueError("max_bytes must be positive or None")
        self.client = client or Qdrant(self.settings)
        self.user_id, self.max_bytes = user_id, max_bytes
        self.tenant = hashlib.sha256(json.dumps([self.settings.namespace, user_id]).encode()).hexdigest()
        self.collection = self.settings.collection_prefix + "_records"
        self.lexical_collection = self.settings.collection_prefix + "_lexical"
        self.jobs_collection = self.settings.collection_prefix + "_jobs"
        self.path = self.settings.url + "/" + self.collection
        self._local = threading.local()
        self.client.ensure_collection(self.collection)
        self.client.ensure_collection(self.lexical_collection, lexical=True)
        self.client.ensure_collection(self.jobs_collection)
        self._head_id = self._id("head")
        self.client.upsert(self.collection, [self._point(self._head_id, dict(kind="head", revision=0, operation="", retained_payload_bytes=0,
                                                                          namespace=self.settings.namespace, user_id=user_id))],
                           update_mode="insert_only")

    def _id(self, key):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self.tenant + ":" + key))

    def _point(self, point_id, payload, vector=None):
        return dict(id=point_id, payload=dict(payload, tenant=self.tenant), vector=vector or {})

    def _where(self, *conditions):
        return {"must": [match("tenant", self.tenant)] + list(conditions)}

    def _head(self):
        rows = self.client.retrieve(self.collection, [self._head_id])
        if not rows or rows[0]["payload"].get("tenant") != self.tenant:
            raise QdrantError("missing tenant publication head")
        return rows[0]["payload"]

    def close(self):
        pass

    @contextmanager
    def read_snapshot(self):
        if getattr(self._local, "snapshot", None) is not None:
            yield
            return
        self._local.snapshot = self._head()
        self._local.markers = {}
        try:
            yield
        finally:
            self._local.snapshot = None
            self._local.markers = {}

    @contextmanager
    def transaction(self):
        if getattr(self._local, "write", None) is not None:
            try:
                yield
            except BaseException:
                self._local.failed = True
                raise
            return
        with self.read_snapshot():
            self._local.write = {}
            self._local.failed = False
            try:
                yield
                if self._local.failed:
                    raise WriteConflict("nested write operation was aborted")
                if self._local.write:
                    self._commit(self._local.snapshot, self._local.write)
            finally:
                self._local.write = None

    def _marker(self, operation):
        cache = getattr(self._local, "markers", {})
        if operation not in cache:
            rows = self.client.retrieve(self.collection, [self._id("operation:" + operation)])
            cache[operation] = rows[0]["payload"] if rows and rows[0]["payload"].get("tenant") == self.tenant else {}
        return cache[operation]

    def _visible(self, payload, head):
        if payload.get("revision", 0) > head["revision"]:
            return False
        operation = payload.get("operation")
        return bool(operation and (operation == head.get("operation") or self._marker(operation).get("state") == "committed"))

    def _finalize(self, operation):
        if not operation:
            return
        existing = self._marker(operation)
        if existing.get("state") == "committed":
            return
        marker = dict(existing, state="committed")
        if not marker.get("kind"):
            raise QdrantError("published operation is missing its journal marker")
        self.client.upsert(self.collection, [self._point(self._id("operation:" + operation), marker)])
        getattr(self._local, "markers", {})[operation] = marker

    def _commit(self, head, changes):
        # Repair a crash between publication and marker finalization before advancing the head.
        self._finalize(head.get("operation"))
        if self._head()["revision"] != head["revision"]:
            raise WriteConflict("tenant changed while preparing the write")
        operation = str(uuid.uuid4())
        revision = head["revision"] + 1
        marker = dict(kind="operation", operation=operation, revision=revision, state="prepared", keys=list(changes))
        records, sparse_points, dense = [], [], {}
        for key, value in changes.items():
            kind = key.split(":", 1)[0]
            payload = dict(kind=kind, key=key, revision=revision, operation=operation,
                           deleted=value is None, data=value)
            if value:
                for field in ("status", "session_id", "conversation_id", "subject", "attribute", "content_hash"):
                    if field in value:
                        payload[field] = value[field]
                if "episode_id" in value:
                    payload["episode_id"] = str(value["episode_id"])
                if "source_ids" in value:
                    payload["source_ids"] = [str(i) for i in value["source_ids"]]
                for field in ("alias_keys", "entity_ids", "subject_id", "object_id"):
                    if field in value:
                        payload[field] = value[field]
                if "fact_id" in value:
                    payload["fact_id"] = str(value["fact_id"])
            pid = self._id(operation + ":" + key)
            records.append(self._point(pid, payload))
            if value and kind in ("fact", "episode", "episode_chunk"):
                text = self._index_text(kind, value)
                lexical = lexical_vector(text)
                if lexical["indices"]:
                    sparse_points.append(self._point(pid, payload, {"lexical": lexical}))
                vec = value.get("vector")
                if vec:
                    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vec):
                        raise ValueError("nonfinite embedding")
                    dense.setdefault(len(vec), []).append(self._point(pid, payload, {"dense": vec}))
        growth = sum(len(json.dumps(p["payload"], ensure_ascii=False).encode()) for p in records) + len(json.dumps(marker).encode())
        retained_bytes = head.get("retained_payload_bytes", 0) + growth
        if self.max_bytes is not None:
            relief = all(value is None or key.split(":", 1)[0] in ("meta", "access", "session", "backlog", "conversation_exclusion", "repair_request")
                         or value.get("status") in (FORGOTTEN, EPISODE_WITHHELD, "archived", FACT_SUPERSEDED)
                         for key, value in changes.items())
            if retained_bytes > self.max_bytes and not relief:
                raise StorageLimitExceeded("tenant retained payload exceeds max_storage_bytes; export and compact or raise the limit")
        self.client.upsert(self.collection, [self._point(self._id("operation:" + operation), marker)])
        for start in range(0, len(records), 128):
            self.client.upsert(self.collection, records[start:start + 128])
        for start in range(0, len(sparse_points), 128):
            self.client.upsert(self.lexical_collection, sparse_points[start:start + 128])
        for dimension, points in dense.items():
            collection = self.settings.collection_prefix + "_dense_" + str(dimension)
            self.client.ensure_collection(collection, dimension)
            for start in range(0, len(points), 128):
                self.client.upsert(collection, points[start:start + 128])
        new_head = dict(head, kind="head", revision=revision, operation=operation, retained_payload_bytes=retained_bytes,
                        fact_revision=revision if any(key.startswith("fact:") for key in changes) else head.get("fact_revision", 0),
                        backlog_revision=revision if any(key.startswith("backlog:") for key in changes) else head.get("backlog_revision", 0))
        # update_only is essential: a missing head must never bypass the precondition.
        self.client.upsert(self.collection, [self._point(self._head_id, new_head)],
                           update_filter=self._where(match("revision", head["revision"])), update_mode="update_only")
        actual = self._head()
        if actual.get("operation") != operation and self._marker(operation).get("state") != "committed":
            raise WriteConflict("another writer published first; this staged operation is invisible")
        self._finalize(operation)

    def _scan(self, *conditions):
        offset = None
        while True:
            page = self.client.scroll(self.collection, self._where(*conditions), offset=offset)
            yield from page["points"]
            offset = page.get("next_page_offset")
            if offset is None:
                break

    def _records(self, kind, ids=None, **fields):
        with self.read_snapshot():
            conditions = [match("kind", kind)]
            keys = None if ids is None else {kind + ":" + str(i) for i in ids}
            if keys is not None:
                if not keys:
                    return []
                conditions.append(match("key", keys))
            # Lifecycle/status filtering is applied AFTER selecting the latest revision.
            # Filtering it before selection can resurrect an obsolete current fact.
            if fields and keys is None:
                field_conditions = [match(k, str(v) if k == "episode_id" else v) for k, v in fields.items()]
                keys = {point["payload"]["key"] for point in self._scan(*(conditions + field_conditions))}
                keys.update(key for key, value in (getattr(self._local, "write", None) or {}).items()
                            if key.startswith(kind + ":") and value and all(value.get(k) == v for k, v in fields.items()))
                if not keys:
                    return []
                conditions.append(match("key", keys))
            latest = {}
            for point in self._scan(*conditions):
                p = point["payload"]
                if self._visible(p, self._local.snapshot) and (p["key"] not in latest or p["revision"] > latest[p["key"]]["revision"]):
                    latest[p["key"]] = p
            records = {key: copy.deepcopy(p["data"]) for key, p in latest.items() if not p["deleted"]}
            for key, value in (getattr(self._local, "write", None) or {}).items():
                if key.split(":", 1)[0] != kind or keys is not None and key not in keys:
                    continue
                if value is None:
                    records.pop(key, None)
                elif all(value.get(k) == v for k, v in fields.items()):
                    records[key] = copy.deepcopy(value)
                else:
                    records.pop(key, None)
            return [value for value in records.values() if all(value.get(k) == v for k, v in fields.items())]

    def record(self, kind, item_id):
        if kind == "job":
            points = self.client.retrieve(self.jobs_collection, [self._id("job:" + str(item_id))])
            if not points or points[0]["payload"].get("tenant") != self.tenant:
                return None
            p = points[0]["payload"]
            return dict(p["data"], _version=p["revision"])
        rows = self._records(kind, [item_id])
        return rows[0] if rows else None

    def records(self, kind, **fields):
        if kind == "job":
            offset, rows = None, []
            while True:
                page = self.client.scroll(self.jobs_collection, self._where(match("kind", "job")), offset=offset)
                rows.extend(dict(p["payload"]["data"], _version=p["payload"]["revision"]) for p in page["points"])
                offset = page.get("next_page_offset")
                if offset is None:
                    return [r for r in rows if all(r.get(k) == v for k, v in fields.items())]
        return self._records(kind, **fields)

    def matching_records(self, kind, field, values, limit):
        """Bounded graph-key lookup with latest-revision and tenant visibility checks.

        Returns (rows, limited). Index matches from obsolete revisions cannot
        resurrect a deleted or changed entity/edge. Never indexes graph records
        into the dense or lexical fact candidate collections.
        """
        if kind not in ('entity', 'relation') or field not in ('alias_keys', 'entity_ids', 'fact_id'):
            raise ValueError('unsupported relation lookup')
        values = {str(value) for value in values}
        if not values or limit <= 0:
            return [], False
        def matches(row):
            value = row.get(field)
            return bool(values & {str(v) for v in (value if isinstance(value, list) else [value])})
        with self.read_snapshot():
            keys, limited = set(), False
            scan_limit = max(256, limit * 8)
            for i, point in enumerate(self._scan(match('kind', kind), match(field, sorted(values)))):
                keys.add(point['payload']['key'].split(':', 1)[1])
                if i + 1 >= scan_limit:
                    limited = True
                    break
            pending = getattr(self._local, 'write', None) or {}
            keys.update(key.split(':', 1)[1] for key, row in pending.items()
                        if key.startswith(kind + ':') and row and matches(row))
            rows = [row for row in self._records(kind, keys) if matches(row)]
            rows.sort(key=lambda row: str(row['id']))
            return rows[:limit], limited or len(rows) > limit

    def put_record(self, kind, item_id, value):
        if kind == "job":
            # A job lease is a single-point CAS independent of memory publication.
            # Heartbeats cannot invalidate a large memory commit's publication head.
            if value is None:
                raise ValueError("job removal requires explicit maintenance")
            data = copy.deepcopy(value)
            expected = data.pop("_version", None)
            version = 0 if expected is None else expected + 1
            operation = str(uuid.uuid4())
            payload = dict(kind="job", key=str(item_id), revision=version, operation=operation,
                           status=data["status"], data=data)
            pid = self._id("job:" + str(item_id))
            self.client.upsert(self.jobs_collection, [self._point(pid, payload)],
                update_filter=self._where(match("revision", expected)) if expected is not None else None,
                update_mode="update_only" if expected is not None else "insert_only")
            # Qdrant's JSON round trip can alter a timestamp's final decimal.
            # Compare the CAS version and unique write identity, not float data:
            # a false conflict here strands an acquired task until lease expiry.
            points = self.client.retrieve(self.jobs_collection, [pid])
            actual = points[0]["payload"] if points else {}
            if (actual.get("tenant") != self.tenant or actual.get("revision") != version
                    or actual.get("operation") != operation):
                raise WriteConflict("job lease changed")
            return
        if getattr(self._local, "write", None) is None:
            with self.transaction():
                self.put_record(kind, item_id, value)
        else:
            self._local.write[kind + ":" + str(item_id)] = copy.deepcopy(value)

    def delete_record(self, kind, item_id):
        self.put_record(kind, item_id, None)

    def _with_access(self, kind, rows):
        accesses = {r["key"]: r for r in self.records("access")}
        for row in rows:
            access = accesses.get(kind + ":" + str(row["id"]))
            if access:
                row.update({k: v for k, v in access.items() if k != "key"})
        return rows

    @staticmethod
    def _new_id():
        return uuid.uuid4().int & ((1 << 53) - 1)

    @staticmethod
    def _index_text(kind, value):
        if kind == "fact":
            from .fact_content import fact_text
            return fact_text(value)
        if kind == "episode_chunk":
            return value["text"]
        view = value.get("safe_view") or value
        return view.get("user_text", "") + "\n" + view.get("assistant_text", "")

    def get_meta(self, key, default=None):
        if key in ("revision", "fact_revision", "backlog_revision"):
            return str((getattr(self._local, "snapshot", None) or self._head()).get(key, 0))
        row = self.record("meta", key)
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.put_record("meta", key, {"value": value})

    def check_embedding_model(self, name):
        stored = self.get_meta("embedding_model")
        if stored is None:
            self.set_meta("embedding_model", name)
        elif stored != name:
            raise EmbeddingMismatch("indexed with %r, configured %r; reindex first" % (stored, name))

    @retry_write
    @atomic
    def create_session(self, opened_at, external_id=None):
        if external_id is not None and (not isinstance(external_id, str) or not external_id):
            raise ValueError("external_id must be a nonempty string or null")
        existing = self.find_open_session(external_id) if external_id else None
        if existing:
            return existing
        sid = "s-" + str(uuid.uuid4())
        self.put_record("session", sid, dict(id=sid, external_id=external_id, opened_at=to_iso(opened_at),
                        closed_at=None, status="open", turns_done=0, next_idx=0))
        return sid

    def session(self, session_id):
        value = self.record("session", session_id)
        if value is None:
            raise KeyError(session_id)
        return value

    def external_ids(self):
        return [r["external_id"] for r in self.records("session") if r.get("external_id") is not None]

    def find_open_session(self, external_id):
        rows = [r for r in self.records("session") if r.get("external_id") == external_id and r["status"] == "open"]
        return max(rows, key=lambda r: r["opened_at"])["id"] if rows else None

    def open_sessions(self):
        return [r["id"] for r in sorted(self.records("session"), key=lambda r: r["opened_at"]) if r["status"] in ("open", "closing")]

    @retry_write
    @atomic
    def request_close(self, session_id, at):
        row = self.session(session_id)
        if row["status"] != "open":
            return
        row.update(status="closing", close_requested_at=to_iso(at))
        self.put_record("session", session_id, row)

    @retry_write
    @atomic
    def append_turn(self, session_id, role, content, at, idempotency_key=None):
        if role not in ("user", "assistant") or not isinstance(content, str):
            raise ValueError("invalid turn")
        session = self.session(session_id)
        dedup_key = session_id + ":" + idempotency_key if idempotency_key else None
        previous = self.record("turn_receipt", dedup_key) if dedup_key else None
        if previous:
            if previous["role"] != role or previous["content_sha256"] != hashlib.sha256(content.encode()).hexdigest():
                raise ValueError("idempotency key reused for a different message")
            return previous["idx"]
        if session["status"] != "open":
            raise ValueError("session is closed; open a new session")
        idx = session["next_idx"]
        self.put_record("turn", session_id + ":" + str(idx),
                        dict(session_id=session_id, idx=idx, role=role, content=content, at=to_iso(at)))
        session["next_idx"] += 1
        self.put_record("session", session_id, session)
        if dedup_key:
            self.put_record("turn_receipt", dedup_key, dict(idx=idx, role=role, content_sha256=hashlib.sha256(content.encode()).hexdigest()))
        return idx

    def turns(self, session_id, start=0):
        return sorted((r for r in self.records("turn", session_id=session_id) if r["idx"] >= start), key=lambda r: r["idx"])

    def last_turn(self, session_id):
        rows = self.turns(session_id)
        return rows[-1] if rows else None

    @atomic
    def mark_consolidated(self, session_id, turns_done, closed_at=None, expected_start=None):
        row = self.session(session_id)
        if expected_start is not None and row["turns_done"] != expected_start:
            raise WriteConflict("session was consolidated by another worker")
        if turns_done < row["turns_done"] or turns_done > row["next_idx"]:
            raise WriteConflict("invalid consolidation cursor")
        if closed_at is not None and turns_done != row["next_idx"]:
            raise WriteConflict("new messages arrived while closing the session")
        row["turns_done"] = turns_done
        if closed_at is not None:
            row.update(status="closed", closed_at=to_iso(closed_at))
        self.put_record("session", session_id, row)

    def history_tokens(self, counter=None):
        count = counter.count if counter else estimate_tokens
        return sum(count(r["content"]) for r in self.records("turn"))

    def conversation_turns(self, session_id):
        row = self.record("conversation_exclusion", session_id)
        rows = self.turns(session_id, row["upto_turn"] if row else 0)
        for turn in rows:
            turn["content"] = turn.get("safe_content", turn["content"])
        return rows

    def _exclude_conversation(self, episode_ids):
        for sid in {e.session_id for e in self.episodes(episode_ids)}:
            row = self.record("conversation_exclusion", sid) or {"upto_turn": 0}
            row["upto_turn"] = max(row["upto_turn"], self.session(sid)["turns_done"])
            self.put_record("conversation_exclusion", sid, row)

    def next_round_idx(self, session_id):
        return max((r["round_idx"] for r in self.records("episode", session_id=session_id)), default=-1) + 1

    @atomic
    def add_episode(self, session_id, round_idx, at, user_text, assistant_text, salience, status):
        rows = [r for r in self.records("episode", session_id=session_id) if r["round_idx"] == round_idx]
        if rows:
            raise WriteConflict("round already consolidated")
        eid = self._new_id()
        self.put_record("episode", eid, dict(id=eid, session_id=session_id, round_idx=round_idx, at=to_iso(at),
                        user_text=user_text, assistant_text=assistant_text, salience=salience, status=status,
                        access_count=0, last_access=None))
        return eid

    @staticmethod
    def _episode(r):
        view = r.get("safe_view") or r
        return Episode(r["id"], r["session_id"], r["round_idx"], from_iso(r["at"]), view["user_text"],
                       view["assistant_text"], r["salience"], r["status"], r.get("access_count", 0), from_iso(r.get("last_access")))

    def episodes(self, ids=None, statuses=None):
        rows = self._with_access("episode", self._records("episode", ids))
        return [self._episode(r) for r in sorted(rows, key=lambda r: (r["at"], r["id"])) if statuses is None or r["status"] in statuses]

    @atomic
    def add_fact(self, *, text, kind, subject, attribute, value, event, recorded_at, valid_from,
                 session_id, importance, confidence=.8, uncertain=False, episode_ids=(), semantics=None, relations=None):
        fid = self._new_id()
        self.put_record("fact", fid, dict(id=fid, text=text, content_hash=hashlib.sha256(text.strip().encode()).hexdigest(),
                        kind=kind, subject=subject, attribute=attribute, value=value,
                        event_start=event.start.isoformat() if event else None, event_end=event.end.isoformat() if event else None,
                        precision=event.precision if event else None, recorded_at=to_iso(recorded_at),
                        valid_from=valid_from.isoformat() if valid_from else None, valid_to=None, status=FACT_CURRENT,
                        superseded_by=None, uncertain=bool(uncertain), confidence=confidence, importance=importance,
                        session_id=session_id, mentions=1, access_count=0, last_access=None,
                        source_ids=sorted(set(episode_ids)), semantics=normalize_semantics(semantics) if semantics else {},
                        relations=copy.deepcopy(relations or []), spans=[]))
        return fid

    @staticmethod
    def _fact(r):
        event = DateSpan(date.fromisoformat(r["event_start"]), date.fromisoformat(r["event_end"]), r["precision"]) if r.get("event_start") else None
        return Fact(r["id"], r["text"], r["kind"], r["subject"], r["attribute"], r["value"], event,
                    from_iso(r["recorded_at"]), date.fromisoformat(r["valid_from"]) if r.get("valid_from") else None,
                    date.fromisoformat(r["valid_to"]) if r.get("valid_to") else None, r["status"], r.get("superseded_by"),
                    bool(r["uncertain"]), r["confidence"], r["importance"], r["session_id"], r.get("mentions", 1),
                    r.get("access_count", 0), from_iso(r.get("last_access")), r.get("semantics") or {}, r.get('relations') or [])

    def facts(self, ids=None, statuses=None):
        return [self._fact(r) for r in sorted(self._with_access("fact", self._records("fact", ids)), key=lambda r: r["id"]) if statuses is None or r["status"] in statuses]

    def fact(self, fact_id):
        row = self.record("fact", fact_id)
        if row is None:
            raise KeyError(fact_id)
        return self._fact(self._with_access("fact", [row])[0])

    def exact_fact(self, text, *, identity=None):
        """Exact-text dedup in the current tenant, excluding hidden/outdated records.

        New records use an indexed hash. Legacy records are found by an exact
        nested text filter and validated against their latest committed revision.
        """
        text = text.strip()
        digest = hashlib.sha256(text.encode()).hexdigest()
        from .fact_content import fact_identity
        def same(row):
            return (row["text"].strip() == text and row["status"] in (FACT_CURRENT, "archived")
                    and (identity is None or fact_identity(self._fact(row)) == identity))
        with self.read_snapshot():
            rows = self.records("fact", content_hash=digest)
            matches = [r for r in rows if same(r)]
            if not matches:
                ids = {p["payload"]["key"].split(":", 1)[1]
                       for p in self._scan(match("kind", "fact"), match("data.text", text))}
                matches = [r for r in self._records("fact", ids) if same(r)]
            return self._fact(min(matches, key=lambda r: r["id"])) if matches else None

    def slot_facts(self, subject, attribute):
        return [self._fact(r) for r in self.records("fact", subject=subject, attribute=attribute) if r["status"] == FACT_CURRENT]

    @atomic
    def _patch(self, kind, item_id, **changes):
        row = self.record(kind, item_id)
        if row is None:
            raise KeyError(item_id)
        row.update(changes)
        self.put_record(kind, item_id, row)

    @atomic
    def add_sources(self, fact_id, episode_ids):
        row = self.record("fact", fact_id)
        row["source_ids"] = sorted(set(row.get("source_ids", [])) | set(episode_ids))
        self.put_record("fact", fact_id, row)

    def sources(self, fact_ids):
        ids = list(fact_ids)
        rows = {r["id"]: r for r in self._records("fact", ids)}
        return {i: list(rows.get(i, {}).get("source_ids", [])) for i in ids}

    def facts_of_episodes(self, episode_ids):
        ids = list(episode_ids)
        if not ids:
            return {}
        out = {i: [] for i in ids}
        # Sources are immutable links except explicit maintenance; latest records are checked.
        with self.read_snapshot():
            keys = {p["payload"]["key"].split(":", 1)[1] for p in self._scan(match("kind", "fact"), match("source_ids", [str(i) for i in ids]))}
            for row in self._records("fact", keys):
                for eid in row.get("source_ids", []):
                    if eid in out:
                        out[eid].append(row["id"])
            for key, row in (getattr(self._local, "write", None) or {}).items():
                if key.startswith("fact:") and row:
                    for eid in row.get("source_ids", []):
                        if eid in out and row["id"] not in out[eid]:
                            out[eid].append(row["id"])
        return out

    def supersede(self, old_id, new_id, valid_to):
        self._patch("fact", old_id, status=FACT_SUPERSEDED, superseded_by=new_id, valid_to=valid_to.isoformat() if valid_to else None)

    def clear_uncertain(self, fact_id):
        self._patch("fact", fact_id, uncertain=False)

    def set_confidence(self, fact_id, confidence):
        self._patch("fact", fact_id, confidence=min(max(confidence, 0), 1))

    @atomic
    def note_duplicate(self, fact_id, episode_ids, confidence):
        row = self.record("fact", fact_id)
        if set(episode_ids) - set(row.get("source_ids", [])):
            row.update(mentions=row["mentions"] + 1, confidence=min(max(confidence, 0), 1))
        row["source_ids"] = sorted(set(row.get("source_ids", [])) | set(episode_ids))
        self.put_record("fact", fact_id, row)

    def chain(self, fact_id):
        facts = {f.id: f for f in self.facts()}
        if fact_id not in facts:
            raise KeyError(fact_id)
        back = {f.superseded_by: f.id for f in facts.values() if f.superseded_by}
        seen = set()
        while fact_id in back:
            if fact_id in seen:
                raise ValueError("cycle in fact version chain")
            seen.add(fact_id)
            fact_id = back[fact_id]
        result, seen = [], set()
        while fact_id is not None:
            if fact_id in seen or fact_id not in facts:
                raise ValueError("invalid fact version chain")
            seen.add(fact_id)
            result.append(facts[fact_id])
            fact_id = facts[fact_id].superseded_by
        return result

    @atomic
    def record_span(self, fact_id, episode_id, role, quote):
        if role not in ("user", "assistant") or not isinstance(quote, str) or not quote.strip():
            return False
        fact, episode = self.record("fact", fact_id), self.record("episode", episode_id)
        if not fact or not episode or episode_id not in fact.get("source_ids", []):
            return False
        text = episode[role + "_text"]
        if text.count(quote) != 1:
            return False
        start = text.index(quote)
        span = dict(episode_id=episode_id, role=role, start=start, end=start + len(quote), quote=quote)
        if span not in fact["spans"]:
            fact["spans"].append(span)
            self.put_record("fact", fact_id, fact)
        return True

    @atomic
    def put_vectors(self, kind, items):
        for item_id, vector in items:
            if not vector or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector):
                raise ValueError("invalid embedding")
            norm = math.sqrt(sum(v*v for v in vector)) or 1
            self._patch(kind, item_id, vector=[v/norm for v in vector])

    def vectors(self, kind, statuses):
        """Maintenance export only. Normal retrieval uses vector_search on the server."""
        rows = self.records(kind)
        if kind == "episode_chunk":
            alive = {e.id for e in self.episodes(statuses=statuses)}
            rows = [r for r in rows if r["episode_id"] in alive]
        else:
            rows = [r for r in rows if r["status"] in statuses]
        rows = sorted((r for r in rows if r.get("vector")), key=lambda r: r["id"])
        matrix = VectorRows()
        for r in rows:
            matrix.append(r["vector"])
        return [r["id"] for r in rows], matrix

    def _search_index(self, collection, query, using, kind, statuses, limit):
        if limit <= 0:
            return []
        with self.read_snapshot():
            where = self._where(match("kind", kind), match("status", statuses),
                                {"key": "revision", "range": {"lte": self._local.snapshot["revision"]}})
            found = {}
            # Old revisions can occupy ANN candidates; validate against the latest record.
            # Finite overfetch is explicit and never described as complete enumeration.
            for fetch in sorted(set((min(max(limit * 4, 32), 10000), min(max(limit * 16, 128), 10000)))):
                points = self.client.query(collection, query, using, where, fetch)
                keys = [p["payload"]["key"].split(":", 1)[1] for p in points]
                current = {kind + ":" + str(r["id"]): r for r in self._records(kind, keys)}
                parents = {r["id"]: r for r in self._records("episode", {r["episode_id"] for r in current.values()})} if kind == "episode_chunk" else {}
                for p in points:
                    payload = p["payload"]
                    row = current.get(payload["key"])
                    if not row or not self._visible(payload, self._local.snapshot) or row != payload["data"]:
                        continue
                    status = parents.get(row["episode_id"], {}).get("status") if kind == "episode_chunk" else row["status"]
                    if status in statuses:
                        found[row["id"]] = max(found.get(row["id"], -math.inf), p["score"])
                if len(found) >= limit or len(points) < fetch:
                    break
            return sorted(found.items(), key=lambda p: (-p[1], p[0]))[:limit]

    def vector_search(self, kind, vector, statuses, limit, threshold=None):
        result = self._search_index(self.settings.collection_prefix + "_dense_" + str(len(vector)), list(vector), "dense", kind, statuses, limit)
        return [(i, score) for i, score in result if threshold is None or score >= threshold]

    def fts_search(self, kind, query, statuses, limit):
        sparse = lexical_vector(query.replace('"', '').replace(' OR ', ' '))
        return self._search_index(self.lexical_collection, sparse, "lexical", kind, statuses, limit) if sparse["indices"] else []

    def episode_chunks(self, ids):
        return [EpisodeChunk(r["id"], r["episode_id"], r["ordinal"], r["role"], r["start"], r["end"])
                for r in sorted(self._records("episode_chunk", ids), key=lambda r: (r["episode_id"], r["ordinal"]))]

    @atomic
    def put_episode_chunks(self, episode_id, chunks):
        for row in self.records("episode_chunk", episode_id=episode_id):
            self.delete_record("episode_chunk", row["id"])
        episode = self.episodes([episode_id])[0]
        originals = {"User": episode.user_text, "Assistant": episode.assistant_text}
        for ordinal, (role, start, end, vector) in enumerate(chunks):
            if role not in originals or not 0 <= start < end <= len(originals[role]):
                raise ValueError("invalid chunk position or role")
            cid = self._new_id()
            self.put_record("episode_chunk", cid, dict(id=cid, episode_id=episode_id, ordinal=ordinal, role=role,
                start=start, end=end, status=episode.status, text=role + ": " + originals[role][start:end]))
            self.put_vectors("episode_chunk", [(cid, vector)])

    @atomic
    def set_status(self, kind, ids, status):
        for item_id in ids:
            changes = {"status": status}
            if kind == "fact" and status == FORGOTTEN:
                changes["semantics"] = {}
            self._patch(kind, item_id, **changes)
            if kind == "episode":
                for row in self.records("episode_chunk", episode_id=item_id):
                    self._patch("episode_chunk", row["id"], status=status)

    @retry_write
    @atomic
    def touch(self, kind, ids, at):
        for item_id in ids:
            row = self.record(kind, item_id)
            if row and row["status"] != FORGOTTEN:
                access = self.record("access", kind + ":" + str(item_id)) or {"access_count": row.get("access_count", 0)}
                self.put_record("access", kind + ":" + str(item_id), dict(key=kind + ":" + str(item_id), access_count=access["access_count"] + 1, last_access=to_iso(at)))

    @atomic
    def add_backlog(self, episode_ids):
        for eid in episode_ids:
            self.put_record("backlog", eid, {"episode_id": eid})

    def backlog(self):
        return self.episodes([r["episode_id"] for r in self.records("backlog")], statuses=("active", "archived", "skipped"))

    def backlog_count(self):
        return len(self.backlog())

    @atomic
    def clear_backlog(self, episode_ids):
        for eid in episode_ids:
            self.delete_record("backlog", eid)
            if self.record("repair_request", eid):
                self._patch("repair_request", eid, status="completed")

    @atomic
    def add_pending(self, new_fact_id, old_fact_id, at, reason):
        pid = self._new_id()
        self.put_record("pending", pid, dict(id=pid, new_fact_id=new_fact_id, old_fact_id=old_fact_id,
                         created_at=to_iso(at), reason=reason, resolution=None))
        return pid

    def pending(self):
        rows = self.records("pending")
        facts = {f.id: f for f in self.facts({i for r in rows for i in (r["new_fact_id"], r["old_fact_id"])})}
        return [r for r in rows if r["resolution"] is None and all(i in facts and facts[i].status != FORGOTTEN for i in (r["new_fact_id"], r["old_fact_id"]))]

    @atomic
    def resolve_pending(self, pending_id, resolution):
        row = next((r for r in self.pending() if r["id"] == pending_id), None)
        if row is None:
            raise KeyError(pending_id)
        self._patch("pending", pending_id, resolution=resolution)
        return row

    def counts(self):
        with self.read_snapshot():
            return {kind: dict(Counter(r["status"] for r in self.records(kind))) for kind in ("fact", "episode", "session")}

    @atomic
    def forget(self, kind, item_id):
        if kind not in ("fact", "episode") or self.record(kind, item_id) is None:
            raise KeyError(item_id)
        self.set_status(kind, [item_id], FORGOTTEN)
        forgotten = [("F" if kind == "fact" else "E") + str(item_id)]
        if kind == "episode":
            episode_ids = [item_id]
            linked = self.facts_of_episodes(episode_ids)[item_id]
            orphans = [fid for fid, sources in self.sources(linked).items() if not self._alive("episode", sources)]
            self.set_status("fact", orphans, FORGOTTEN)
            forgotten.extend("F" + str(fid) for fid in orphans)
        else:
            episode_ids = self.sources([item_id])[item_id]
            fine = self._fine_forget(item_id, episode_ids)
            shared = [e.id for e in self.episodes(episode_ids) if e.status != FORGOTTEN]
            if not fine:
                self.set_status("episode", shared, EPISODE_WITHHELD)
            orphans = [] if fine else [eid for eid, facts in self.facts_of_episodes(episode_ids).items() if not self._alive("fact", facts)]
            self.set_status("episode", orphans, FORGOTTEN)
            forgotten.extend("E" + str(eid) for eid in orphans)
        if kind == "episode" or not fine:
            self._exclude_conversation(episode_ids)
        self.clear_backlog(episode_ids)
        return forgotten

    def _fine_forget(self, fact_id, episode_ids):
        import re
        fact = self.record("fact", fact_id)
        anchor = fact["value"].strip()
        if not episode_ids or len(anchor) < 2 or anchor == fact["text"].strip():
            return False
        pattern = re.compile(re.escape(anchor), re.I)
        for eid in episode_ids:
            raw = self.record("episode", eid)
            spans = [span for span in fact.get("spans", []) if span["episode_id"] == eid]
            if not raw or raw["status"] in (FORGOTTEN, EPISODE_WITHHELD) or not spans or not any(pattern.search(span["quote"]) for span in spans):
                return False
            if any(raw[span["role"] + "_text"][span["start"]:span["end"]] != span["quote"] for span in spans):
                return False
            siblings = self.facts(self.facts_of_episodes([eid])[eid])
            if any(f.id != fact_id and f.status != FORGOTTEN and pattern.search(f.text) for f in siblings):
                return False
        for episode in self.episodes(episode_ids):
            row = self.record("episode", episode.id)
            row["safe_view"] = dict(user_text=pattern.sub("[forgotten]", episode.user_text),
                                    assistant_text=pattern.sub("[forgotten]", episode.assistant_text))
            row.pop("vector", None)
            self.put_record("episode", episode.id, row)
            self.put_episode_chunks(episode.id, [])
            done = self.session(episode.session_id)["turns_done"]
            for turn in self.turns(episode.session_id):
                if turn["idx"] < done and pattern.search(turn.get("safe_content", turn["content"])):
                    turn["safe_content"] = pattern.sub("[forgotten]", turn.get("safe_content", turn["content"]))
                    self.put_record("turn", episode.session_id + ":" + str(turn["idx"]), turn)
        return True

    @classmethod
    def users(cls, settings=None, client=None):
        """Administrative namespace inventory, used only for job recovery/inspection."""
        settings = settings or QdrantSettings.environment()
        client = client or Qdrant(settings)
        name = settings.collection_prefix + "_records"
        client.ensure_collection(name)
        offset, found = None, []
        where = {"must": [match("kind", "head"), match("namespace", settings.namespace)]}
        while True:
            page = client.scroll(name, where, offset=offset)
            found.extend(p["payload"]["user_id"] for p in page["points"])
            offset = page.get("next_page_offset")
            if offset is None:
                return sorted(set(found))

    def _alive(self, kind, ids):
        return any(r["status"] != FORGOTTEN for r in self._records(kind, ids))

    def allocated_bytes(self):
        # Retained tenant payload estimate, not the Qdrant server's physical disk allocation.
        return (getattr(self._local, "snapshot", None) or self._head()).get("retained_payload_bytes", 0)

    def storage_usage(self):
        return dict(backend="qdrant", committed_payload_estimate_bytes=self.allocated_bytes(), max_bytes=self.max_bytes,
                    physical_bytes=None, note="committed payload estimate excludes uncommitted staging, ANN indexes and server overhead")

    def backup(self, path):
        destination = Path(path)
        if destination.exists():
            raise ValueError("backup requires a new file path")
        with self.read_snapshot():
            rows = [p for p in self._scan() if p["payload"].get("kind") in ("head", "operation") or self._visible(p["payload"], self._local.snapshot)]
            with destination.open("x", encoding="utf-8") as stream:
                json.dump(dict(format="memsys-qdrant-export-v1", tenant=self.tenant, head=self._local.snapshot,
                               points=rows, jobs=self.records("job")), stream, ensure_ascii=False)

    def index_source_fingerprint(self):
        with self.read_snapshot():
            facts = [[f.id, f.text, f.status, f.superseded_by, self.sources([f.id])[f.id]] for f in self.facts()]
            episodes = [[e.id, e.session_id, e.round_idx, to_iso(e.at), e.status, e.user_text, e.assistant_text] for e in self.episodes()]
            return hashlib.sha256(json.dumps([facts, episodes], sort_keys=True).encode()).hexdigest()

    def check_index_fingerprint(self, fingerprint, *, legacy=False):
        manifest = json.loads(self.get_meta("index_manifest") or "{}")
        if manifest.get("strategy", "legacy") != "legacy":
            raise EmbeddingMismatch("retired parent chunk index; run reindex")
        stored = self.get_meta("index_fingerprint")
        if stored and stored != fingerprint:
            raise EmbeddingMismatch("index configuration changed; run reindex")

    def retire_chunk_schema(self):
        pass

    def _index_payload(self):
        return dict(records={kind: self.records(kind) for kind in ("fact", "episode", "episode_chunk")},
                    meta={key: self.get_meta(key) for key in ("embedding_model", "index_manifest", "index_fingerprint")})

    def _save_index_generation(self):
        gid = self._new_id()
        self.put_record("index_generation", gid, dict(id=gid, fingerprint=self.get_meta("index_fingerprint"),
                        source_sha256=self.index_source_fingerprint(), payload=self._index_payload()))
        return gid

    def index_generations(self):
        return [{k: r[k] for k in ("id", "fingerprint", "source_sha256")} for r in self.records("index_generation")]

    @atomic
    def replace_vectors(self, embedding_model, items, chunks=None, *, manifest=None, expected_source=None):
        if expected_source is not None and self.index_source_fingerprint() != expected_source:
            raise EmbeddingMismatch("sources changed during reindex")
        vectors = [v for entries in items.values() for _, v in entries] + [e[3] for entries in (chunks or {}).values() for e in entries]
        if vectors and (len({len(v) for v in vectors}) != 1 or not all(v and all(math.isfinite(x) for x in v) for v in vectors)):
            raise ValueError("invalid embedding dimensions")
        previous = self._save_index_generation()
        for kind in ("fact", "episode"):
            for row in self.records(kind):
                row.pop("vector", None)
                self.put_record(kind, row["id"], row)
        for row in self.records("episode_chunk"):
            self.delete_record("episode_chunk", row["id"])
        for kind, entries in items.items():
            self.put_vectors(kind, entries)
        for eid, entries in (chunks or {}).items():
            self.put_episode_chunks(eid, entries)
        self.set_meta("embedding_model", embedding_model)
        for key in ("index_manifest", "index_fingerprint"):
            self.delete_record("meta", key)
        if manifest:
            self.set_meta("index_manifest", json.dumps(manifest, sort_keys=True))
            self.set_meta("index_fingerprint", manifest["fingerprint"])
        self.validate_index()
        self.set_meta("index_source_sha256", self.index_source_fingerprint())
        current = self._save_index_generation()
        self.set_meta("active_index_generation", str(current))
        self.set_meta("previous_index_generation", str(previous))
        for row in self.records("index_generation"):
            if row["id"] not in (previous, current):
                self.delete_record("index_generation", row["id"])

    @atomic
    def rollback_index(self, generation=None):
        gid = int(generation or self.get_meta("previous_index_generation") or 0)
        saved = self.record("index_generation", gid)
        if not saved:
            raise ValueError("no retained index generation")
        if saved["source_sha256"] != self.index_source_fingerprint():
            raise EmbeddingMismatch("sources or lifecycle changed; reindex instead")
        previous = self._save_index_generation()
        for kind in ("fact", "episode"):
            indexed = {r["id"]: r for r in saved["payload"]["records"][kind]}
            for row in self.records(kind):
                row.pop("vector", None)
                if indexed.get(row["id"], {}).get("vector"):
                    row["vector"] = indexed[row["id"]]["vector"]
                self.put_record(kind, row["id"], row)
        for row in self.records("episode_chunk"):
            self.delete_record("episode_chunk", row["id"])
        for row in saved["payload"]["records"]["episode_chunk"]:
            self.put_record("episode_chunk", row["id"], row)
        for key, value in saved["payload"]["meta"].items():
            self.delete_record("meta", key)
            if value is not None:
                self.set_meta(key, value)
        self.validate_index()
        self.set_meta("active_index_generation", str(gid))
        self.set_meta("previous_index_generation", str(previous))
        return gid

    def validate_index(self):
        covered = {}
        episodes = {e.id: e for e in self.episodes(statuses=("active", "archived"))}
        for row in self.records("episode_chunk"):
            episode = episodes.get(row["episode_id"])
            if not episode:
                continue
            text = episode.user_text if row["role"] == "User" else episode.assistant_text
            if not 0 <= row["start"] < row["end"] <= len(text) or not row.get("vector"):
                raise ValueError("invalid chunk source range or missing vector")
            covered.setdefault((episode.id, row["role"]), []).append((row["start"], row["end"]))
        for eid in {key[0] for key in covered}:
            for role, text in (("User", episodes[eid].user_text), ("Assistant", episodes[eid].assistant_text)):
                end = 0
                for left, right in sorted(covered.get((eid, role), [])):
                    if left > end:
                        raise ValueError("source coverage has a gap")
                    end = max(end, right)
                if end != len(text):
                    raise ValueError("chunks do not cover source")
        return dict(valid=True, covered_roles=len(covered))

    def index_coverage(self):
        rows = [r for r in self.records("episode") if r["status"] in ("active", "archived") and r.get("vector")]
        ids = {r["id"] for r in rows}
        chunks = [r for r in self.records("episode_chunk") if r["episode_id"] in ids and r.get("vector")]
        chunked = {r["episode_id"] for r in chunks}
        return dict(indexed_episodes=len(ids), chunked_episodes=len(chunked), chunk_vectors=len(chunks), legacy_prefix_episodes=len(ids - chunked))

    def compact(self, *, offline=False):
        """Remove superseded storage revisions with ALL clients stopped (not fact history)."""
        if not offline or getattr(self._local, "write", None) is not None:
            raise ValueError("compaction requires offline maintenance outside a write operation")
        with self.read_snapshot():
            points = list(self._scan())
            latest = {}
            for p in points:
                data = p["payload"]
                if data.get("key") and self._visible(data, self._local.snapshot):
                    if data["key"] not in latest or data["revision"] > latest[data["key"]]["payload"]["revision"]:
                        latest[data["key"]] = p
            keep = {p["id"] for p in latest.values() if not p["payload"]["deleted"]}
            operations = {p["payload"]["operation"] for p in latest.values() if p["id"] in keep}
            if self._local.snapshot.get("operation"):
                operations.add(self._local.snapshot["operation"])
            keep.update(self._id("operation:" + op) for op in operations)
            keep.add(self._head_id)
            collections = self.client.request("GET", "/collections")["collections"]
            deleted = 0
            for item in collections:
                name = item["name"]
                if name != self.collection and name != self.lexical_collection and not name.startswith(self.settings.collection_prefix + "_dense_"):
                    continue
                removed, offset = [], None
                while True:
                    page = self.client.scroll(name, self._where(), offset=offset)
                    removed.extend(p["id"] for p in page["points"] if p["id"] not in keep)
                    offset = page.get("next_page_offset")
                    if offset is None:
                        break
                for start in range(0, len(removed), 128):
                    self.client.delete(name, self._where({"has_id": removed[start:start + 128]}))
                deleted += len(removed)
            retained = sum(len(json.dumps(p["payload"], ensure_ascii=False).encode()) for p in points if p["id"] in keep and p["id"] != self._head_id)
            head = dict(self._local.snapshot, retained_payload_bytes=retained)
            self.client.upsert(self.collection, [self._point(self._head_id, head)],
                               update_filter=self._where(match("revision", head["revision"])), update_mode="update_only")
            return dict(removed_points=deleted, physical_disk_reclamation="asynchronous Qdrant optimizer")
