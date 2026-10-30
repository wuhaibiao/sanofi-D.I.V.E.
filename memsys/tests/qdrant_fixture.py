"""Protocol test double: no SQLite, real LLM, model downloads or performance measurement."""
import copy
import math
import threading
from memsys.qdrant import QdrantError


def accepts(point, where):
    def condition(c):
        if "has_id" in c:
            return point["id"] in c["has_id"]
        value = point["payload"]
        for part in c.get("key", "").split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if "match" in c:
            if "any" in c["match"]:
                return bool(set(value if isinstance(value, list) else [value]) & set(c["match"]["any"]))
            return c["match"]["value"] in value if isinstance(value, list) else value == c["match"]["value"]
        if "range" in c:
            return value is not None and all(value <= v if k == "lte" else value >= v for k, v in c["range"].items())
        raise AssertionError(c)
    return all(condition(c) for c in where.get("must", [])) and not any(condition(c) for c in where.get("must_not", []))


class Engine:
    def __init__(self):
        self.collections = {}
        self.lock = threading.RLock()
        self.queries = []
        self.fail_stage = False
        self.fail_finalize = False
        self.before_publish = None

    def ensure_collection(self, name, dimension=None, lexical=False):
        with self.lock:
            self.collections.setdefault(name, {})

    def upsert(self, collection, points, update_filter=None, update_mode=None):
        if self.before_publish and update_filter:
            hook, self.before_publish = self.before_publish, None
            hook()
        with self.lock:
            table = self.collections[collection]
            for point in points:
                old = table.get(point["id"])
                if update_mode == "insert_only" and old or update_mode == "update_only" and not old:
                    continue
                if old and update_filter and not accepts(old, update_filter):
                    continue
                if self.fail_stage and point["payload"].get("key"):
                    raise QdrantError("injected stage failure")
                if self.fail_finalize and point["payload"].get("state") == "committed":
                    raise QdrantError("injected finalization failure")
                table[point["id"]] = copy.deepcopy(point)
        return {"status": "completed"}

    def retrieve(self, collection, ids):
        with self.lock:
            return [copy.deepcopy(self.collections[collection][i]) for i in ids if i in self.collections[collection]]

    def scroll(self, collection, where, limit=256, offset=None):
        with self.lock:
            items = sorted((copy.deepcopy(p) for p in self.collections[collection].values() if accepts(p, where)), key=lambda p: p["id"])
            if offset:
                items = [p for p in items if p["id"] >= offset]
            return {"points": items[:limit], "next_page_offset": items[limit]["id"] if len(items) > limit else None}

    def query(self, collection, query, using, where, limit):
        self.queries.append(dict(collection=collection, filter=copy.deepcopy(where), limit=limit, using=using))
        if collection not in self.collections:
            return []
        with self.lock:
            hits = []
            for p in self.collections[collection].values():
                if not accepts(p, where) or using not in p["vector"]:
                    continue
                vec = p["vector"][using]
                if using == "dense":
                    n = math.sqrt(sum(x*x for x in query)) or 1
                    score = sum(x*y for x, y in zip(query, vec))/n
                else:
                    q, v = dict(zip(query["indices"], query["values"])), dict(zip(vec["indices"], vec["values"]))
                    score = sum(value*v.get(i, 0) for i, value in q.items())
                    if not score:
                        continue
                hits.append(dict(copy.deepcopy(p), score=score))
            return sorted(hits, key=lambda p: -p["score"])[:limit]

    def delete(self, collection, where):
        with self.lock:
            self.collections[collection] = {i: p for i, p in self.collections[collection].items() if not accepts(p, where)}

    def request(self, method, path, body=None, **kwargs):
        if path == "/collections":
            return {"collections": [{"name": name} for name in self.collections]}
        raise AssertionError((method, path))
