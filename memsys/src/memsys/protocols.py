"""Responses 与 Messages 的标准库适配器，共用 HTTP 重试、额度分类与用量账本。"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Sequence

from .anthropic_client import split_system
from .llm import ChatResult, Message, ModelError, OpenAICompatClient, strip_reasoning


class ResponsesClient(OpenAICompatClient):
    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        inputs = [dict(message) for message in messages]
        for message in inputs:
            if message.get("role") == "assistant":
                # ChatModel stores completed answers; preserve an explicit phase on replay.
                message.setdefault("phase", "final_answer")
        body: Dict[str, Any] = {"model": self.config.model, "input": inputs, "store": False}
        if max_tokens is not None:
            body["max_output_tokens"] = max_tokens + self.config.output_token_headroom
        if json_mode and self.config.json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        # 推理型号不一定支持 temperature；需要时由型号的 extra_body 显式配置。
        body.update(self.config.extra_body)
        data, stats = self._post("/responses", body, tags, purpose=purpose)
        error = data.get("error")
        if error or data.get("status") == "failed":
            code = error.get("code") if isinstance(error, dict) else None
            status = 429 if code == "rate_limit_exceeded" else 402 if code == "insufficient_quota" else None
            raise ModelError("response failed: %s" % json.dumps(error)[:400], status=status)
        output = data.get("output") or []
        if not isinstance(output, list):
            raise ModelError("response output must be a list")
        messages_out = [item for item in output if isinstance(item, dict) and item.get("type") == "message"]
        final_messages = [item for item in messages_out if item.get("phase") == "final_answer"]
        if final_messages:
            # Intermediate commentary can contain drafts and JSON. It is never the answer.
            messages_out = final_messages[-1:]
        elif any(item.get("phase") for item in messages_out):
            messages_out = []
        # Providers without phase retain the legacy output_text behavior.
        text = "".join(block.get("text", "") for item in messages_out
                       for block in item.get("content", [])
                       if isinstance(block, dict) and block.get("type") == "output_text")
        refusal = any(block.get("type") == "refusal" for item in messages_out
                      for block in item.get("content", []) if isinstance(block, dict))
        if refusal:
            text += "".join(block.get("refusal", "") for item in messages_out
                            for block in item.get("content", [])
                            if isinstance(block, dict) and block.get("type") == "refusal")
        if not text and not refusal and data.get("status") != "incomplete":
            raise ModelError("%s returned no response text" % self.config.base_url)
        usage = data.get("usage") or {}
        prompt, completion = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        cost = self.config.pricing.cost(prompt, completion, cached)
        self._record(purpose, prompt, completion, cached, cost, stats, bool(usage), tags)
        incomplete = data.get("status") == "incomplete"
        detail = (data.get("incomplete_details") or {}).get("reason")
        reason = ("content_filter" if incomplete and detail == "content_filter" else
                  "refusal" if refusal else "length" if incomplete and detail in (None, "max_output_tokens") else
                  "incomplete" if incomplete else "stop")
        return ChatResult(strip_reasoning(text), self.config.model, prompt, completion, cached,
                          cost, stats.latency_s, reason)


class AnthropicMessagesClient(OpenAICompatClient):
    """OpenCode 的 Messages 路径直接使用 HTTP，避免依赖可选 SDK。"""

    def _headers(self, tags: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers = super()._headers(tags)
        authorization = headers.pop("Authorization", None)
        if authorization:
            headers["x-api-key"] = authorization[len("Bearer "):]
        headers["anthropic-version"] = "2023-06-01"
        return headers

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        system, rest = split_system(messages)
        body: Dict[str, Any] = {"model": self.config.model, "messages": rest,
                               "max_tokens": (max_tokens or 4096) + self.config.output_token_headroom}
        if system:
            body["system"] = system
        # Messages 没有通用 json_object 格式；记忆提示词本身要求输出 JSON。
        body.update(self.config.extra_body)
        data, stats = self._post("/messages", body, tags, purpose=purpose)
        if not isinstance(data.get("content"), list):
            raise ModelError("%s returned no message content: %s" % (self.config.base_url, json.dumps(data)[:300]))
        text = "".join(block.get("text", "") for block in data["content"]
                       if isinstance(block, dict) and block.get("type") == "text")
        usage = data.get("usage") or {}
        # Anthropic 输入字段不包含缓存读取和写入；账本统一成完整输入与其中缓存命中。
        cached = int(usage.get("cache_read_input_tokens") or 0)
        prompt = int(usage.get("input_tokens") or 0) + cached + int(usage.get("cache_creation_input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
        cost = self.config.pricing.cost(prompt, completion, cached)
        self._record(purpose, prompt, completion, cached, cost, stats, bool(usage), tags)
        reason = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                  "tool_use": "tool_calls", "refusal": "refusal"}.get(data.get("stop_reason"), "stop")
        return ChatResult(strip_reasoning(text), self.config.model, prompt, completion, cached,
                          cost, stats.latency_s, reason)
