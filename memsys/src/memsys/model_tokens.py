"""模型专用 token 计数；不改变回答上下文的 UTF-8/估算预算。"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from pathlib import Path


def pinned_revision(model, revision):
    if not Path(model).expanduser().is_dir() and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("a remote model/tokenizer requires an immutable 40-character revision")


class ModelTokenizer:
    def __init__(self, model, revision, path=""):
        self.model, self.revision = model, revision
        self.local = bool(path)
        if path:
            try:
                from tokenizers import Tokenizer
            except ImportError:
                raise ValueError("install memsys[strict] for tokenizer.json") from None
            file = Path(path).expanduser().resolve()
            self.tokenizer = Tokenizer.from_file(str(file))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
            self.sha256 = hashlib.sha256(file.read_bytes()).hexdigest()
        else:
            pinned_revision(model, revision)
            try:
                from transformers import AutoTokenizer
            except ImportError as exc:
                raise ValueError("model tokenizer unavailable (%s); install memsys[rerank]" % exc) from exc
            self.tokenizer = AutoTokenizer.from_pretrained(str(Path(model).expanduser()) if Path(model).expanduser().is_dir() else model,
                                                          revision=revision, use_fast=True, trust_remote_code=False)
            if not self.tokenizer.is_fast:
                raise ValueError("a fast tokenizer is required for reproducible source offsets")
            self.sha256 = hashlib.sha256(self.tokenizer.backend_tokenizer.to_str().encode()).hexdigest()

    def count(self, text, special=True):
        if self.local:
            return len(self.tokenizer.encode(text, add_special_tokens=special).ids)
        return len(self.tokenizer.encode(text, add_special_tokens=special, truncation=False))

    def pair_count(self, question, text):
        if self.local:
            return len(self.tokenizer.encode(question, text, add_special_tokens=True).ids)
        return len(self.tokenizer.encode(question, text, add_special_tokens=True, truncation=False))

    def manifest(self):
        return {"model": self.model, "revision": self.revision, "sha256": self.sha256,
                "unit": "model_tokens", "includes_special_tokens": True}


class PairEncodingCache:
    """Bounded, model-local single-sequence encodings; never cache relevance scores.

    The independent backend has no mutable padding/truncation configuration from
    the HF batching wrapper. Full passages and final windows survive requests;
    binary-search probes live only in a small per-document memo.
    """
    def __init__(self, tokenizer, max_entries=256, max_tokens=65536):
        from tokenizers import Tokenizer
        source = tokenizer.tokenizer if tokenizer.local else tokenizer.tokenizer.backend_tokenizer
        self.backend = Tokenizer.from_str(source.to_str())
        self.backend.no_padding()
        self.backend.no_truncation()
        self.max_entries, self.max_tokens = max_entries, max_tokens
        self.cache, self.tokens = OrderedDict(), 0
        self.hits = self.misses = self.encoded_characters = self.encoded_texts = 0
        self.special_pair = self.backend.num_special_tokens_to_add(is_pair=True)

    @staticmethod
    def key(text):
        return hashlib.sha256(text.encode('utf-8')).digest()

    def lookup(self, key):
        found = self.cache.get(key)
        if found is not None:
            self.cache.move_to_end(key)
            self.hits += 1
        return found

    def remember(self, key, encoded):
        size = len(encoded.ids)
        if self.max_entries < 1 or size > self.max_tokens or key in self.cache:
            return
        while self.cache and (len(self.cache) >= self.max_entries or self.tokens + size > self.max_tokens):
            _, old = self.cache.popitem(last=False)
            self.tokens -= len(old.ids)
        self.cache[key] = encoded
        self.tokens += size

    def encode(self, text, persist=True):
        key = self.key(text)
        encoded = self.lookup(key)
        if encoded is None:
            self.misses += 1
            self.encoded_texts += 1
            self.encoded_characters += len(text)
            encoded = self.backend.encode(text, add_special_tokens=False)
        if persist:
            self.remember(key, encoded)
        return encoded

    def clear(self):
        self.cache.clear()
        self.tokens = 0

    def info(self):
        return dict(entries=len(self.cache), tokens=self.tokens, max_entries=self.max_entries,
                    max_tokens=self.max_tokens, hits=self.hits, misses=self.misses,
                    encoded_texts=self.encoded_texts, encoded_characters=self.encoded_characters)

    def document(self, query, prefix, text):
        return PairDocumentEncoding(self, query, prefix, text)


class PairDocumentEncoding:
    def __init__(self, cache, query, prefix, text):
        self.cache, self.query, self.prefix = cache, query, prefix
        self.query_size = len(query.ids)
        self.memo = OrderedDict()
        self.memo_tokens = 0
        # Reuse a fact's full encoding even when the next question differs.
        cache.encode(prefix + text)

    def passage(self, piece, persist=False):
        text = self.prefix + piece
        key = self.cache.key(text)
        encoded = self.memo.get(key)
        if encoded is not None:
            self.memo.move_to_end(key)
        else:
            encoded = self.cache.encode(text, persist=False)
            size = len(encoded.ids)
            if size <= 8192:
                while self.memo and (len(self.memo) >= 32 or self.memo_tokens + size > 8192):
                    _, old = self.memo.popitem(last=False)
                    self.memo_tokens -= len(old.ids)
                self.memo[key] = encoded
                self.memo_tokens += size
        if persist:
            self.cache.remember(key, encoded)
        return encoded

    def count(self, piece):
        return self.query_size + len(self.passage(piece).ids) + self.cache.special_pair

    def prepare(self, piece, input_names):
        passage = self.passage(piece, persist=True)
        pair = self.cache.backend.post_process(self.query, passage, add_special_tokens=True)
        expected = self.query_size + len(passage.ids) + self.cache.special_pair
        if len(pair.ids) != expected:
            raise ValueError('pair postprocessor changed sequence lengths unexpectedly')
        fields = dict(input_ids=pair.ids, attention_mask=pair.attention_mask, token_type_ids=pair.type_ids)
        return {name: fields[name] for name in input_names if name in fields}


def structure_spans(text):
    """连续、无损句段；代码围栏和列表条目优先保持结构。"""
    spans, offset, fenced = [], 0, False
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        if line.lstrip().startswith("```"):
            fenced = not fenced
            spans.append((offset, end))
        elif fenced or re.match(r"\s*(?:[-*+]\s|\d+[.)、]\s|#{1,6}\s)", line):
            spans.append((offset, end))
        else:
            start = offset
            for match in re.finditer(r"[。！？]|[.!?](?=\s|$)", line):
                cut = offset + match.end()
                spans.append((start, cut)); start = cut
            if start < end:
                spans.append((start, end))
        offset = end
    return spans


def bounded_spans(text, count, limit, overlap=0, start=0, end=None):
    """每个窗口以实际完整输入核验；区间保留所有空白与 Unicode 字符。"""
    end = len(text) if end is None else end
    if count("") >= limit:
        raise ValueError("token limit leaves no space after query/prefix and special tokens")
    boundaries = sorted({b for _, b in structure_spans(text[start:end])})
    boundaries = [start + b for b in boundaries]
    current = start
    while current < end:
        if count(text[current:end]) <= limit:
            yield current, end
            return
        lo, hi = current + 1, end
        if count(text[current:lo]) > limit:
            raise ValueError("token budget cannot encode one source character")
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count(text[current:mid]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        natural = [b for b in boundaries if current < b <= cut]
        if natural and natural[-1] - current >= max((cut - current) // 2, 1):
            cut = natural[-1]
        while count(text[current:cut]) > limit:
            cut -= 1
        yield current, cut
        next_start = cut
        if overlap:
            # 完整句优先；达不到目标时保留可追溯后缀，且每轮必须前进。
            choices = [b for b in boundaries if current < b < cut]
            baseline = count("")
            for boundary in choices:
                if count(text[boundary:cut]) - baseline <= overlap:
                    next_start = boundary
                    break
            if next_start == cut:
                while next_start > current + 1 and count(text[next_start - 1:cut]) - baseline <= overlap:
                    next_start -= 1
        current = max(current + 1, next_start)
