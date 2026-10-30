"""Remote document reranking; the server owns tokenization and model loading."""
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import asdict
from .reranking import RerankFailure


class RemoteReranker:
    def __init__(self, settings):
        self.settings = settings

    def rank(self, question, documents):
        s = self.settings
        if not s.rerank_url:
            raise RerankFailure("remote rerank_url is required")
        body = dict(query=question, documents=[asdict(doc) for doc in documents])
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(s.rerank_api_key_env)
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(s.rerank_url.rstrip("/") + "/rerank", json.dumps(body, ensure_ascii=False).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=s.rerank_timeout_s) as response:
                data = json.load(response)
            scores = data["scores"]
            expected = {doc.key for doc in documents}
            if not isinstance(scores, dict) or set(scores) != expected or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in scores.values()):
                raise ValueError("reranker returned invalid or incomplete scores")
            return scores, dict(data.get("trace") or {}, backend="remote", url=s.rerank_url)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RerankFailure("remote reranking failed: %s" % str(exc)[:300]) from None
