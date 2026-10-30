"""可审计的文本预算。默认以 UTF-8 字节数保守限制输入，不把估算冒充精确分词。"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

from .textutil import estimate_tokens, split_tokens, truncate_tokens


class TokenCounter:
    def __init__(self, mode="utf8", tokenizer_path=""):
        if mode not in ("utf8", "estimate", "tokenizer"):
            raise ValueError("budget_mode must be utf8, estimate or tokenizer")
        self.mode, self.tokenizer = mode, None
        self.sha256 = None
        if mode == "tokenizer":
            path = Path(tokenizer_path).expanduser().resolve()
            if not tokenizer_path or not path.is_file():
                raise ValueError("tokenizer mode requires a local budget_tokenizer_path")
            try:
                from tokenizers import Tokenizer
            except ImportError:
                raise ValueError("install memsys[strict] to use a local tokenizer") from None
            self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            self.tokenizer = Tokenizer.from_file(str(path))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()

    def count(self, text: str) -> int:
        if self.tokenizer is not None:
            return len(self.tokenizer.encode(text, add_special_tokens=False).ids)
        return len(text.encode("utf-8")) if self.mode == "utf8" else estimate_tokens(text)

    def split(self, text: str, limit: int):
        return split_tokens(text, limit, count=self.count)

    def truncate(self, text: str, limit: int):
        return truncate_tokens(text, limit, count=self.count)

    def messages(self, messages: Sequence[Mapping[str, str]], reserve_per_message=8) -> int:
        # 内容与角色标签之外的服务端聊天模板另留固定余量，trace 明确披露该口径。
        return sum(self.count(m.get("content", "")) + self.count(m.get("role", ""))
                   + reserve_per_message for m in messages)

    def manifest(self):
        return {"mode": self.mode, "tokenizer_sha256": self.sha256,
                "unit": "utf8_bytes" if self.mode == "utf8" else "tokens",
                "scope": "text content; message counts reserve 8 units per message for framing"}


@lru_cache(maxsize=16)
def counter_for(mode="utf8", tokenizer_path="", version=None):
    return TokenCounter(mode, tokenizer_path)


def budget_counter(settings):
    version = None
    if settings.budget_mode == "tokenizer":
        path = Path(settings.budget_tokenizer_path).expanduser().resolve()
        if path.is_file():
            stat = path.stat()
            version = (stat.st_mtime_ns, stat.st_size)
    return counter_for(settings.budget_mode, settings.budget_tokenizer_path, version)
