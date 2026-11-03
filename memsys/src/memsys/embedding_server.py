"""Local OpenAI-compatible dense BGE-M3 service; loading/inference only on explicit requests."""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class BGEEmbedding:
    def __init__(self, model_dir, device="mps", batch_size=2):
        self.model_dir = Path(model_dir)
        self.device, self.batch_size = device, batch_size
        self.model = self.tokenizer = self.torch = None
        self.lock = threading.Lock()

    @property
    def files_ready(self):
        return (all((self.model_dir / name).is_file() for name in ("config.json", "tokenizer.json"))
                and any((self.model_dir / name).is_file() for name in ("model.safetensors", "pytorch_model.bin")))

    def _load(self):
        if self.model is not None:
            return
        if not self.files_ready:
            raise RuntimeError("local BGE-M3 weights are not prepared")
        import torch
        from transformers import AutoModel, AutoTokenizer
        if self.device == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("MPS is unavailable; configure CPU explicitly")
        tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir), local_files_only=True, trust_remote_code=False)
        model = AutoModel.from_pretrained(str(self.model_dir), local_files_only=True, trust_remote_code=False,
            use_safetensors=(self.model_dir / "model.safetensors").is_file(), weights_only=True, torch_dtype=torch.float32)
        model.to(self.device).eval()
        self.tokenizer, self.model, self.torch = tokenizer, model, torch

    def embed(self, texts):
        with self.lock:
            self._load()
            encoded = self.tokenizer(texts, padding=False, truncation=False)
            lengths = [len(ids) for ids in encoded["input_ids"]]
            if any(size > 8192 for size in lengths):
                raise ValueError("BGE-M3 input exceeds 8192 tokens; no input was truncated")
            vectors = [None] * len(texts)
            order = sorted(range(len(texts)), key=lambda i: lengths[i])
            offset = 0
            with self.torch.inference_mode():
                while offset < len(order):
                    # Long inputs run individually; normal role segments can share a batch.
                    size = 1 if lengths[order[offset]] > 1024 else self.batch_size
                    indices = order[offset:offset + size]
                    if any(lengths[i] > 1024 for i in indices):
                        indices = [order[offset]]
                    rows = [{key: value[i] for key, value in encoded.items()} for i in indices]
                    batch = self.tokenizer.pad(rows, padding=True, return_tensors="pt").to(self.device)
                    output = self.model(**batch).last_hidden_state[:, 0]
                    output = self.torch.nn.functional.normalize(output, p=2, dim=1).float().cpu().tolist()
                    for i, vector in zip(indices, output):
                        vectors[i] = vector
                    offset += len(indices)
            return vectors, sum(lengths)


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

    def authorize(self):
        if self.server.token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.token):
            self.reply(401, {"error": {"message": "unauthorized"}})
            return False
        return True

    def do_GET(self):
        if self.path == "/healthz":
            ready = self.server.backend.files_ready
            self.reply(200 if ready else 503, {"status": "ready" if ready else "weights_missing",
                "model_loaded": self.server.backend.model is not None, "model_files_ready": ready})
        elif self.path == "/v1/models" and self.authorize():
            self.reply(200, {"object": "list", "data": [{"id": "bge-m3", "object": "model", "owned_by": "local"}]})
        else:
            if self.path != "/v1/models":
                self.reply(404, {"error": {"message": "unknown endpoint"}})

    def do_POST(self):
        if not self.authorize():
            return
        if self.path != "/v1/embeddings":
            self.reply(404, {"error": {"message": "unknown endpoint"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4 * 1024 * 1024:
                raise ValueError("invalid request size")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or data.get("model") != "bge-m3":
                raise ValueError("model must be bge-m3")
            texts = data.get("input")
            if isinstance(texts, str):
                texts = [texts]
            if not isinstance(texts, list) or not 1 <= len(texts) <= 256 or not all(isinstance(t, str) for t in texts):
                raise ValueError("input must contain 1 to 256 strings")
            if data.get("encoding_format", "float") != "float" or data.get("dimensions", 1024) != 1024:
                raise ValueError("only float encoding and 1024 dimensions are supported")
            vectors, tokens = self.server.backend.embed(texts)
            self.reply(200, {"object": "list", "model": "bge-m3",
                "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens}})
        except (ValueError, TypeError, KeyError) as exc:
            self.reply(400, {"error": {"message": str(exc)}})
        except Exception as exc:
            self.reply(503, {"error": {"message": str(exc)[:300]}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--device", choices=("mps", "cpu"), default="mps")
    args = parser.parse_args()
    token = os.environ.get("EMBEDDING_API_KEY", "")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        parser.error("EMBEDDING_API_KEY is required for a network listener")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.token, server.backend = token, BGEEmbedding(args.model_dir, args.device)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
