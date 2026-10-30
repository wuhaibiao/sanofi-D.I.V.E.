"""Small server-only Qdrant REST transport; never uses Qdrant local/SQLite mode."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

from .textutil import terms


class QdrantError(RuntimeError):
    pass


@dataclass
class QdrantSettings:
    url: str = "http://127.0.0.1:6333"
    collection_prefix: str = "memsys"
    namespace: str = "default"
    api_key_env: str = "QDRANT_API_KEY"
    timeout_s: float = 30.0

    @classmethod
    def from_dict(cls, data):
        value = cls(**{k: v for k, v in data.items() if not k.startswith("_")})
        parsed = urlsplit(value.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("storage.url must be an HTTP(S) server URL without credentials")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", value.collection_prefix):
            raise ValueError("invalid Qdrant collection_prefix")
        if not value.namespace or not isinstance(value.namespace, str):
            raise ValueError("storage.namespace must be a nonempty string")
        if isinstance(value.timeout_s, bool) or not isinstance(value.timeout_s, (int, float)) or not math.isfinite(value.timeout_s) or value.timeout_s <= 0:
            raise ValueError("storage.timeout_s must be positive")
        return value

    @classmethod
    def environment(cls):
        return cls.from_dict(dict(url=os.environ.get("QDRANT_URL", cls.url),
                                 namespace=os.environ.get("MEMSYS_NAMESPACE", cls.namespace),
                                 collection_prefix=os.environ.get("MEMSYS_COLLECTION_PREFIX", cls.collection_prefix)))


class Qdrant:
    def __init__(self, settings=None):
        self.settings = settings or QdrantSettings.environment()
        self._known = set()
        self._version_checked = False

    def request(self, method, path, body=None, *, missing=False):
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.settings.api_key_env)
        if key:
            headers["api-key"] = key
        req = urllib.request.Request(self.settings.url.rstrip("/") + path, encoded, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_s) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            detail = exc.read().decode(errors="replace")[:800]
            raise QdrantError("Qdrant HTTP %s: %s" % (exc.code, detail)) from None
        except (OSError, ValueError) as exc:
            raise QdrantError("Qdrant server unavailable at %s: %s" % (self.settings.url, exc)) from None
        return data.get("result", data)

    def ensure_collection(self, name, dimension=None, lexical=False):
        if name in self._known:
            return
        if not self._version_checked:
            info = self.request("GET", "/")
            version = str(info.get("version", ""))
            numbers = re.match(r"^(\d+)\.(\d+)", version)
            if not numbers or tuple(map(int, numbers.groups())) < (1, 16):
                raise QdrantError("Qdrant >=1.16 is required for conditional publication; server version: " + version)
            self._version_checked = True
        existing = self.request("GET", "/collections/" + name, missing=True)
        if existing is None:
            spec = {"vectors": {"dense": {"size": dimension, "distance": "Cosine"}} if dimension else {}}
            if lexical:
                spec["sparse_vectors"] = {"lexical": {"modifier": "idf"}}
            try:
                self.request("PUT", "/collections/" + name, spec)
            except QdrantError:
                if self.request("GET", "/collections/" + name, missing=True) is None:
                    raise
        for field, schema in (("tenant", {"type": "keyword", "is_tenant": True}),
                              ("kind", "keyword"), ("key", "keyword"), ("operation", "keyword"),
                              ("revision", "integer"), ("status", "keyword"), ("session_id", "keyword"), ("conversation_id", "keyword"),
                              ("subject", "keyword"), ("attribute", "keyword"), ("content_hash", "keyword"), ("episode_id", "keyword"),
                              ("source_ids", "keyword"), ("namespace", "keyword"), ("alias_keys", "keyword"),
                              ("entity_ids", "keyword"), ("fact_id", "keyword"), ("subject_id", "keyword"), ("object_id", "keyword")):
            self.request("PUT", "/collections/%s/index?wait=true" % name,
                         {"field_name": field, "field_schema": schema})
        self._known.add(name)

    def upsert(self, collection, points, *, update_filter=None, update_mode=None):
        body = {"points": points}
        if update_filter is not None:
            body["update_filter"] = update_filter
        if update_mode:
            body["update_mode"] = update_mode
        return self.request("PUT", "/collections/%s/points?wait=true&ordering=strong" % collection, body)

    def retrieve(self, collection, ids):
        if not ids:
            return []
        return self.request("POST", "/collections/%s/points?consistency=all" % collection,
                            {"ids": ids, "with_payload": True, "with_vector": False})

    def scroll(self, collection, where, *, limit=256, offset=None):
        body = {"filter": where, "limit": limit, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        return self.request("POST", "/collections/%s/points/scroll?consistency=all" % collection, body)

    def query(self, collection, query, using, where, limit):
        if collection not in self._known:
            if self.request("GET", "/collections/" + collection, missing=True) is None:
                return []
            self._known.add(collection)
        return self.request("POST", "/collections/%s/points/query?consistency=all" % collection,
                            {"query": query, "using": using, "filter": where, "limit": limit,
                             "with_payload": True, "with_vector": False})["points"]

    def delete(self, collection, where):
        return self.request("POST", "/collections/%s/points/delete?wait=true&ordering=strong" % collection,
                            {"filter": where})


def match(key, value):
    return {"key": key, "match": {"any": list(value)} if isinstance(value, (tuple, list, set)) else {"value": value}}


def lexical_vector(text):
    """Deterministic lexical TF + server IDF; Chinese uses the shared term tokenizer.

    This is lexical sparse retrieval, not a claim of equivalence to BM25 or BGE-M3 sparse vectors.
    Hash collisions are possible and are limited to the lexical candidate stage.
    """
    counts = Counter(terms(text))
    values = {}
    for term, count in counts.items():
        idx = int.from_bytes(hashlib.blake2s(term.encode(), digest_size=4).digest(), "big")
        values[idx] = values.get(idx, 0) + 1 + math.log(count)
    indices = sorted(values)
    return {"indices": indices, "values": [values[i] for i in indices]}
