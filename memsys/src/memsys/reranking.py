"""本地 cross-encoder 重排：惰性加载、共享队列、完整配对窗口及显式失败。"""

from __future__ import annotations

import math
try:
    import resource
except ImportError:  # Windows 仍可使用 CPU 后端；无法测量的资源值明确为空。
    resource = None
import sys
import threading
import time
from dataclasses import dataclass

from .model_tokens import ModelTokenizer, PairEncodingCache, bounded_spans, pinned_revision


@dataclass(frozen=True)
class RerankDocument:
    key: str
    text: str
    header: str = ""
    question_context: str = ""
    source_start: int = 0
    source_role: str = ""


class RerankFailure(RuntimeError):
    pass


class BGEReranker:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.model = self.tokenizer = self.torch = None

    def _load(self):
        if self.model is not None:
            return
        s = self.settings
        pinned_revision(s.rerank_model, s.rerank_revision)
        pinned_revision(s.rerank_model, s.rerank_tokenizer_revision)
        import torch
        import transformers
        import tokenizers
        from transformers import AutoModelForSequenceClassification
        device = s.rerank_device
        if device == "auto":
            device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
        if device == "mps" and not torch.backends.mps.is_available():
            raise RerankFailure("MPS is unavailable")
        if device == "cuda" and not torch.cuda.is_available():
            raise RerankFailure("CUDA is unavailable")
        self.tokenizer = ModelTokenizer(s.rerank_model, s.rerank_tokenizer_revision)
        model = AutoModelForSequenceClassification.from_pretrained(s.rerank_model, revision=s.rerank_revision,
            torch_dtype=getattr(torch, s.rerank_dtype), trust_remote_code=False, use_safetensors=True)
        model.to(device).eval()
        self.encodings = PairEncodingCache(self.tokenizer)
        self.device, self.torch, self.model = device, torch, model
        self.dependencies = {"torch":torch.__version__,"transformers":transformers.__version__,
                             "tokenizers":tokenizers.__version__,"python":sys.version.split()[0]}

    def rank(self, question, documents):
        began = time.monotonic()
        with self.lock:
            acquired = time.monotonic()
            try:
                load_started = time.monotonic()
                self._load()
                load_s = time.monotonic() - load_started
                s, tokenizer, torch = self.settings, self.tokenizer, self.torch
                self.encodings.clear()
                cache_before = self.encodings.info()
                prepare_started = time.monotonic()
                query = self.encodings.encode(question)
                features, windows = [], []
                for doc in documents:
                    header = doc.header
                    if doc.question_context:
                        context_end = next(bounded_spans(doc.question_context, tokenizer.count,
                            min(64, s.rerank_max_tokens // 4)))[1]
                        header += "User question: " + doc.question_context[:context_end] + "\n"
                    encoded = self.encodings.document(query, header, doc.text)
                    count = encoded.count
                    for start, end in bounded_spans(doc.text, count, s.rerank_max_tokens, s.rerank_overlap_tokens):
                        text = header + doc.text[start:end]
                        feature = encoded.prepare(doc.text[start:end], tokenizer.tokenizer.model_input_names)
                        windows.append({"key": doc.key, "start": start, "end": end,
                                        "source_start":doc.source_start + start, "source_end":doc.source_start + end,
                                        "source_role":doc.source_role,
                                        "text": text, "pair_tokens": len(feature['input_ids'])})
                        features.append(feature)
                prepare_s = time.monotonic() - prepare_started
                predicted = [None] * len(features)
                # Share computation only for exactly identical model inputs in
                # this request. Preserve every source window and its provenance.
                equivalent, representatives = {}, {}
                for index, feature in enumerate(features):
                    identity = tuple((name, tuple(value)) for name, value in sorted(feature.items()))
                    representative = representatives.setdefault(identity, index)
                    equivalent.setdefault(representative, []).append(index)
                order = sorted(equivalent, key=lambda i: (windows[i]['pair_tokens'], i))
                batch_prepare_s = forward_s = 0.0
                batches = []
                with torch.inference_mode():
                    for offset in range(0, len(order), s.rerank_batch_size):
                        indices = order[offset:offset + s.rerank_batch_size]
                        batch_started = time.monotonic()
                        batch = tokenizer.tokenizer.pad([features[i] for i in indices], padding=True,
                                                        return_tensors="pt", verbose=False)
                        if batch["input_ids"].shape[1] > s.rerank_max_tokens:
                            raise RerankFailure("pair token budget was exceeded; no truncation is allowed")
                        batches.append(dict(windows=len(indices), padded_tokens=int(batch['input_ids'].numel())))
                        batch = {key: value.to(self.device) for key, value in batch.items()}
                        batch_prepare_s += time.monotonic() - batch_started
                        forward_started = time.monotonic()
                        values = self.model(**batch, return_dict=True).logits.view(-1).float().cpu().tolist()
                        forward_s += time.monotonic() - forward_started
                        if len(values) != len(indices):
                            raise RerankFailure('reranker batch score count did not match')
                        for index, value in zip(indices, values):
                            for equivalent_index in equivalent[index]:
                                predicted[equivalent_index] = value
                if len(predicted) != len(windows) or not all(v is not None and math.isfinite(v) for v in predicted):
                    raise RerankFailure("reranker returned invalid window scores")
                finalize_started = time.monotonic()
                scores = {}
                for window, score in zip(windows, predicted):
                    window["score"] = score
                    scores[window["key"]] = max(scores.get(window["key"], -math.inf), score)
                peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None
                peak = int(peak if sys.platform == "darwin" else peak * 1024) if peak is not None else None
                trace = {"backend": "bge", "model": s.rerank_model, "revision": s.rerank_revision,
                         "tokenizer": tokenizer.manifest(), "device": self.device, "dtype": s.rerank_dtype,
                         "batch_size": s.rerank_batch_size, "window_aggregation": "max_logit",
                         "batching": "stable_unique_pair_length_buckets", "pair_windows":len(windows),
                         "inference_windows": len(order), "reused_pair_windows": len(windows) - len(order),
                         "windows": windows, "scores": scores, "queue_s": acquired - began,
                         "inference_and_load_s": time.monotonic() - acquired,
                         "process_peak_rss_bytes": peak, "process_peak_rss_scope": "process lifetime",
                         "device_allocated_bytes": int(torch.mps.current_allocated_memory()) if self.device == "mps" else None}
                cache_after = self.encodings.info()
                trace['tokenization'] = dict(policy='cached_single_sequences_exact_pair_postprocess_v1',
                    **{key:cache_after[key]-cache_before[key] for key in ('hits','misses','encoded_texts','encoded_characters')},
                    cache={key:cache_after[key] for key in ('entries','tokens','max_entries','max_tokens')},
                    scope='request', cleared_after_request=True)
                trace['timings_s'] = dict(load=load_s, tokenize_and_window=prepare_s,
                    batch_prepare=batch_prepare_s, model_and_readback=forward_s,
                    finalize=time.monotonic()-finalize_started, total=time.monotonic()-acquired)
                trace['batches'] = batches
                trace['dependencies'] = self.dependencies
                return scores, trace
            except Exception as exc:
                raise RerankFailure("%s: %s" % (type(exc).__name__, str(exc)[:500])) from exc
            finally:
                # Model/tokenizer weights remain warm; request text and encodings
                # never survive success or failure into another request.
                if hasattr(self, 'encodings'):
                    self.encodings.clear()


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def local_reranker(settings):
    key = tuple(getattr(settings, name) for name in ("rerank_model", "rerank_revision", "rerank_tokenizer_revision",
                "rerank_device", "rerank_dtype", "rerank_batch_size", "rerank_max_tokens", "rerank_overlap_tokens"))
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = BGEReranker(settings)
        return _CACHE[key]
