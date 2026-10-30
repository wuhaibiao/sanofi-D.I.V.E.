"""Internal HTTP wrapper around the existing BGE cross-encoder, run in a separate container."""
import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .config import MemorySettings
from .reranking import RerankDocument, local_reranker


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(200 if self.path == "/healthz" else 404, {"status": "ready", "model_loaded": self.server.ranker.model is not None})

    def do_POST(self):
        expected = self.server.token
        if expected and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + expected):
            self.reply(401, {"error": "unauthorized"})
            return
        if self.path != "/rerank":
            self.reply(404, {"error": "unknown endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4 * 1024 * 1024:
                raise ValueError("invalid request size")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value.get("query"), str) or not isinstance(value.get("documents"), list) or len(value["documents"]) > 256:
                raise ValueError("query and at most 256 documents are required")
            docs = [RerankDocument(**item) for item in value["documents"]]
            if any(not isinstance(doc.text, str) or not isinstance(doc.key, str) for doc in docs):
                raise ValueError("invalid documents")
            scores, trace = self.server.ranker.rank(value["query"], docs)
            # No raw documents in remote response logs/traces.
            trace.pop("windows", None)
            self.reply(200, {"scores": scores, "trace": trace})
        except (ValueError, TypeError, KeyError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception as exc:
            self.reply(503, {"error": str(exc)[:300]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--device", default=os.environ.get("RERANK_DEVICE", "cpu"))
    args = parser.parse_args()
    settings = MemorySettings(rerank_device=args.device)
    token = os.environ.get("RERANK_API_KEY", "")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        parser.error("RERANK_API_KEY is required when binding to a network interface")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.ranker, server.token = local_reranker(settings), token
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
