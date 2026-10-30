"""原文向量的无损分段计划；位置始终指向对应说话人的原文字符区间。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .config import MemorySettings
from .budget import budget_counter


@dataclass(frozen=True)
class ChunkInput:
    role: str
    start: int
    end: int
    vector_index: int


@dataclass
class EpisodeIndex:
    texts: List[str] = field(default_factory=list)
    representatives: Dict[int, int] = field(default_factory=dict)
    chunks: Dict[int, List[ChunkInput]] = field(default_factory=dict)


def episode_index(episodes: Sequence[Tuple[str, str]], settings: MemorySettings) -> EpisodeIndex:
    plan = EpisodeIndex()
    counter = budget_counter(settings)
    for number, (user, assistant) in enumerate(episodes):
        if not settings.episode_chunk_vectors:
            plan.representatives[number] = len(plan.texts)
            plan.texts.append(("User: %s\nAssistant: %s" % (user, assistant))[:settings.embed_chars])
            continue
        if settings.embed_chars < 1 or settings.embed_chunk_tokens < 16:
            raise ValueError("chunk embeddings require embed_chars >= 1 and embed_chunk_tokens >= 16")
        chunks = []
        for role, text in (("User", user), ("Assistant", assistant)):
            start = 0
            # 先限制字符数，再按 token 分段。所有片段拼接后严格等于原文。
            for char_start in range(0, len(text), settings.embed_chars):
                for piece in counter.split(text[char_start:char_start + settings.embed_chars],
                                          settings.embed_chunk_tokens - counter.count(role + ": ") - 2):
                    index = len(plan.texts)
                    plan.texts.append(role + ": " + piece)
                    chunks.append(ChunkInput(role, start, start + len(piece), index))
                    start += len(piece)
        if chunks:
            plan.chunks[number] = chunks
            plan.representatives[number] = chunks[0].vector_index
    return plan


def stored_chunks(plan, number, vectors, offset=0):
    return [(c.role, c.start, c.end, vectors[offset + c.vector_index])
            for c in plan.chunks.get(number, [])]
