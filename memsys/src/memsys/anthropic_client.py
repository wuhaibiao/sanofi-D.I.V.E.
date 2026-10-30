"""Anthropic Messages API 的对话客户端（可选）：只有 key 是 ANTHROPIC_API_KEY、或者代理的上游是 Anthropic 时才用到。

用官方 SDK（pip install anthropic），用到时才导入；记忆核心本身仍然只用标准库。
它满足 llm.ChatModel 协议，用量同样记进 UsageLedger。Anthropic 没有 embedding 接口。

- 系统消息放进 system 参数，其余按原顺序作为 messages。
- 新模型默认开着思考，思考也占 max_tokens：调用方给的 max_tokens 是"回答"的上限，这里另加一段余量，
  并默认把 effort 设成 low（记忆环节都是短小的结构化任务）。ModelConfig.extra_body 可以覆盖。
- 支持服务端兜底的模型默认打开兜底（fallbacks: "default"），被安全分类器拒答时由服务端换模型重答。
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .llm import ChatResult, KeySource, Message, ModelConfig, ModelError, UsageLedger, UsageRecord, strip_reasoning

DEFAULT_BASE_URL = "https://api.anthropic.com"
THINKING_HEADROOM = 4000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "refusal",
           "tool_use": "tool_calls", "pause_turn": "length"}


def split_system(messages: Sequence[Message]) -> Tuple[str, List[Dict[str, str]]]:
    """OpenAI 风格的消息列表 → (system 文本, 只含 user/assistant 的 messages)。"""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    if not rest or rest[0]["role"] != "user":
        rest.insert(0, {"role": "user", "content": "(start)"})
    return system, rest


class AnthropicClient:
    read_budget_managed = True
    def __init__(self, config: ModelConfig, ledger: Optional[UsageLedger] = None,
                 api_key: Optional[KeySource] = None, client: Any = None):
        """client 只在测试里传，替换掉真实的 SDK 客户端。"""
        self.config = config
        self.ledger = ledger if ledger is not None else UsageLedger()
        self._api_key: Optional[KeySource] = api_key
        if api_key is None and config.api_key_env:
            self._api_key = os.environ.get(config.api_key_env)
            if not self._api_key:
                raise ModelError(f"environment variable {config.api_key_env} is not set")
        self._injected = client
        self._client: Any = None
        self._client_key: Optional[str] = None

    def _sdk(self) -> Any:
        if self._injected is not None:
            return self._injected
        key = self._api_key() if callable(self._api_key) else self._api_key
        if self._client is not None and key == self._client_key:
            return self._client
        try:
            import anthropic
        except ImportError:
            raise ModelError("the Anthropic API needs the official SDK: pip install anthropic") from None
        base = self.config.base_url.rstrip("/")
        if base.endswith("/v1"):   # SDK 自己会拼 /v1/messages
            base = base[:-3]
        self._client = anthropic.Anthropic(api_key=key, base_url=base or DEFAULT_BASE_URL,
                                           timeout=self.config.timeout_s, max_retries=self.config.max_retries)
        self._client_key = key
        return self._client

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        system, rest = split_system(messages)
        extra_body: Dict[str, Any] = {"output_config": {"effort": "low"}}
        extra_headers: Dict[str, str] = {}
        if self.config.model in FALLBACK_MODELS and self.config.base_url.rstrip("/").startswith(DEFAULT_BASE_URL):
            extra_body["fallbacks"] = "default"
            extra_headers["anthropic-beta"] = FALLBACK_BETA
        extra_body.update(self.config.extra_body)
        kwargs: Dict[str, Any] = {"model": self.config.model, "messages": rest,
                                  "max_tokens": (max_tokens or 4096) + THINKING_HEADROOM,
                                  "extra_body": extra_body}
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        if system:
            kwargs["system"] = system
        started_at, t0 = time.time(), time.monotonic()
        from .read_budget import current_read_budget
        budget = current_read_budget()
        def send(timeout):
            try:
                client = self._sdk()
                if budget is not None:
                    client = client.with_options(max_retries=0, timeout=timeout)
                return client.messages.create(**kwargs)
            except ModelError:
                raise
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                error = ModelError("%s: %s" % (type(exc).__name__, str(exc)[:300]), status=status)
                error.transport_error = type(exc).__name__ in ('APIConnectionError', 'APITimeoutError')
                raise error from None
        before = budget.additional_retries if budget else 0
        response = (budget.call(send, purpose=purpose, timeout_s=self.config.timeout_s) if budget
                    else send(self.config.timeout_s))
        latency = time.monotonic() - t0
        text = "".join(getattr(block, "text", "") for block in response.content
                       if getattr(block, "type", "") == "text")
        usage = getattr(response, "usage", None)
        prompt = int(getattr(usage, "input_tokens", 0) or 0)
        cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        completion = int(getattr(usage, "output_tokens", 0) or 0)
        # Anthropic 的 input_tokens 不含缓存命中部分，这里统一成"输入总量 + 其中命中缓存"的口径
        cost = self.config.pricing.cost(prompt + cached, completion, cached)
        self.ledger.record(UsageRecord(
            started_at=started_at, model=self.config.model, purpose=purpose, prompt_tokens=prompt + cached,
            completion_tokens=completion, cached_tokens=cached, cost=cost, latency_s=latency,
            retries=budget.additional_retries - before if budget else 0,
            usage_reported=usage is not None, tags=dict(tags or {}),
            cost_known=self.config.pricing.known and (usage is not None or
                       self.config.pricing.input == self.config.pricing.output == 0),
            pricing=asdict(self.config.pricing)))
        return ChatResult(text=strip_reasoning(text), model=self.config.model, prompt_tokens=prompt + cached,
                          completion_tokens=completion, cached_tokens=cached, cost=cost, latency_s=latency,
                          finish_reason=_FINISH.get(getattr(response, "stop_reason", None) or "", "stop"))
