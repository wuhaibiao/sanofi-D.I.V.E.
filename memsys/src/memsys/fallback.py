"""对话模型的额度切换；embedding 保持同一个向量空间。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .llm import ChatModel, ChatResult, Message, ModelError

log = logging.getLogger(__name__)


class QuotaFallbackChat:
    """额度不足或限流时按顺序切换，成功后继续用它；全被限流时等待冷却。"""
    read_budget_managed = True

    def __init__(self, clients: Sequence[ChatModel], names: Sequence[str], cooldown_s: float = 300):
        if not clients or len(clients) != len(names):
            raise ValueError("fallback clients and names must be non-empty and have equal lengths")
        self.clients = tuple(clients)
        self.candidate_names = tuple(names)
        self.cooldown_s = cooldown_s
        self._active = 0
        self._blocked: Dict[int, Tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._session_id = uuid.uuid4().hex

    @property
    def config(self):
        with self._lock:
            return self.clients[self._active].config

    @property
    def active_name(self) -> str:
        with self._lock:
            return self.candidate_names[self._active]

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        call_tags = dict(tags or {})
        call_tags.setdefault("session_id", call_tags.get("question_id") or self._session_id)
        # 冷却期间继续用下一模型，冷却结束后重新尝试更高优先级（海外优先）。
        order = range(len(self.clients))
        failed_statuses = []
        for index in order:
            with self._lock:
                if self._blocked.get(index, (0, 0))[0] > time.monotonic():
                    continue
            try:
                from .read_budget import current_read_budget
                from .read_retry import ReadTransportChat
                client = self.clients[index]
                if current_read_budget() is not None:
                    client = ReadTransportChat(client)
                result = client.chat(messages, purpose=purpose, temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode, tags=call_tags)
            except ModelError as exc:
                from .read_budget import current_read_budget, is_read_service_error
                if current_read_budget() is not None and is_read_service_error(exc):
                    raise
                if not (exc.quota_or_rate_limit or exc.model_unavailable):
                    raise
                failed_statuses.append(exc.status)
                with self._lock:
                    self._blocked[index] = (time.monotonic() + self.cooldown_s, exc.status)
                log.warning("模型 %s 额度不足、限流或访问未开放（HTTP %s），检查下一个候选模型。",
                            self.candidate_names[index], exc.status)
            else:
                with self._lock:
                    self._active = index
                    self._blocked.pop(index, None)
                return result
        with self._lock:
            now = time.monotonic()
            # 并发请求可能在本次失败后成功，清掉最后一个冷却项；仍返回模型错误。
            wait = max(1, int(min((deadline for deadline, _ in self._blocked.values()), default=now) - now) + 1)
            codes = [code for _, code in self._blocked.values()] + failed_statuses
            status = 429 if 429 in codes else 402 if 402 in codes else 403
        raise ModelError("all configured models are unavailable or quota-limited: %s; retry in %ss" % (
            ", ".join(self.candidate_names), wait), status=status)
