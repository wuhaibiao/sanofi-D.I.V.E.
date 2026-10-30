"""模型适配层：项目里唯一直接调用大模型和 embedding 服务的模块。

所有服务商都走 OpenAI 兼容的 HTTP 接口（OpenAI、DeepSeek，以及本地的 Ollama、vLLM 等），
换模型只改配置。记忆核心只依赖下面的 ChatModel / EmbeddingModel 协议，不依赖具体客户端，
这就是手册 B.3 要求的"记忆模块与 LLM 解耦"。

每次调用的 token、费用和耗时都记进 UsageLedger，评估报告要披露的成本从这里汇总。

ModelConfig.from_dict 的输入示例：

    {"model": "qwen2.5:7b-instruct", "base_url": "http://localhost:11434/v1"}

    {"model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1",
     "api_key_env": "DEEPSEEK_API_KEY",
     "pricing": {"input": 0, "output": 0, "cached_input": 0}}

单价按服务商当前的价目表填写（每百万 token）。只用标准库，不需要安装依赖。
"""

from __future__ import annotations

import http.client
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

Message = Dict[str, str]  # {"role": "system" | "user" | "assistant", "content": "..."}
KeySource = Union[str, Callable[[], Optional[str]]]   # 固定的 key，或者每次调用时去取（代理模式借用 agent 的 key）

# 限流、网关超时、服务过载这类暂时性错误值得重试；其余 4xx 多半是请求本身有问题，重试没用
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})   # 529：Anthropic 过载
MAX_RETRY_AFTER_S = 60.0


class ModelError(RuntimeError):
    """调用模型服务失败：不可重试的 HTTP 错误、重试耗尽，或返回内容不是 JSON。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status   # HTTP 状态码；网络错误等没有状态码时为 None

    @property
    def account_problem(self) -> bool:
        """key 无效、余额不足、没有权限：再试多少次都一样，应该停下来让人处理。"""
        return self.status in (401, 402, 403)

    @property
    def quota_or_rate_limit(self) -> bool:
        """只把支付额度和限流错误交给候选模型；鉴权或请求参数错误照常报出。"""
        if self.status in (402, 429):
            return True
        return self.status in (400, 403) and bool(re.search(
            r"insufficient[_ -](?:quota|balance|credit|funds)|quota.{0,40}(?:exceed|exhaust|limit)|"
            r"(?:usage|credit|balance|budget).{0,40}(?:exceed|exhaust|insufficient|limit reached)|"
            r"(?:exceeded|reached).{0,30}(?:usage limit|quota)|额度|余额不足", str(self), re.I))

    @property
    def model_unavailable(self) -> bool:
        """服务商明确停用模型或要求单独开启权限时，可以跳过；普通鉴权错误不跳过。"""
        return self.status in (400, 403, 404) and bool(re.search(
            r"model.{0,60}(?:not found|not available|unavailable|disabled|not enabled|does not exist)|"
            r"(?:unknown|unsupported|disabled) model|model_not_found|"
            r"workspace.{0,30}privacy settings|allow paid endpoints that train|"
            r"(?:enable|allow).{0,50}(?:this model|china.hosted models)", str(self), re.I))


class BadJSONError(ModelError):
    """要求返回 JSON，但解析不出来。text 保留原始回复，方便排查。"""

    def __init__(self, text: str):
        super().__init__(f"model reply is not valid JSON: {text[:300]!r}")
        self.text = text


class IncompleteJSONError(ModelError):
    """The response ended before the requested JSON decision was complete."""

    def __init__(self, finish_reason: str, *, output_limit: Optional[int] = None,
                 completion_tokens: Optional[int] = None):
        detail = (" (output limit=%s, completion tokens=%s)" % (output_limit, completion_tokens)
                  if output_limit is not None else "")
        super().__init__("JSON response was not completed: " + finish_reason + detail)
        self.finish_reason = finish_reason
        self.output_limit = output_limit
        self.completion_tokens = completion_tokens


@dataclass(frozen=True)
class Pricing:
    """每百万 token 的单价。null 表示未知，显式 0 表示免费或本地运行。"""

    input: Optional[float] = None
    output: Optional[float] = None
    cached_input: Optional[float] = None  # 命中提示缓存的输入单价；不填就按 input 计
    currency: Optional[str] = None
    as_of: Optional[str] = None
    source: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("input", "output", "cached_input"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError("pricing.%s must be null or a finite non-negative number" % name)

    @property
    def known(self) -> bool:
        return self.input is not None and self.output is not None

    def cost(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
        if not self.known:
            return 0.0   # 数字字段保持兼容；账本的 cost_known 明确标出未知，不当成零费用
        cached_rate = self.input if self.cached_input is None else self.cached_input
        uncached = max(prompt_tokens - cached_tokens, 0)
        return (uncached * self.input + cached_tokens * cached_rate
                + completion_tokens * self.output) / 1_000_000


@dataclass(frozen=True)
class ModelConfig:
    model: str                         # 服务商那边的模型名
    base_url: str                      # 例如 https://api.openai.com/v1、http://localhost:11434/v1
    api: str = "openai"                # openai：Chat Completions；responses：Responses；anthropic：Messages
    api_key_env: Optional[str] = None  # 存 API key 的环境变量名，本地服务不需要；key 本身不写进配置
    pricing: Pricing = field(default_factory=Pricing)
    timeout_s: float = 120.0
    max_retries: int = 4
    json_mode: bool = True             # 服务端是否支持 response_format={"type": "json_object"}
    embed_batch_size: int = 64
    embed_max_input_bytes: int = 0    # 0 沿用提供方窗口；启用后无损分段并按字节权重合成单位向量
    extra_body: Mapping[str, Any] = field(default_factory=dict)  # 服务商特有参数，例如 {"seed": 42}
    output_token_headroom: int = 0     # 必须思考的型号另外预留输出 token，避免耗尽回答预算

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelConfig:
        """从 JSON/TOML 读出的字典构造；拼错的键会直接报 TypeError。"""
        fields = dict(data)
        known = {f for f in cls.__dataclass_fields__ if f != "pricing"}
        unknown = set(fields) - known - {"pricing"}
        if unknown:
            raise TypeError("unknown model settings: %s (allowed: %s)" % (
                ", ".join(sorted(unknown)), ", ".join(sorted(known | {"pricing"}))))
        if not fields.get("model") or not fields.get("base_url"):
            raise ValueError("a model needs both \"model\" and \"base_url\"")
        if fields.get("api", "openai") not in ("openai", "anthropic", "responses"):
            raise ValueError("api must be openai, anthropic or responses, got %r" % fields["api"])
        headroom = fields.get("output_token_headroom", 0)
        if not isinstance(headroom, int) or isinstance(headroom, bool) or headroom < 0:
            raise ValueError("output_token_headroom must be a non-negative integer")
        embed_limit=fields.get('embed_max_input_bytes',0)
        if isinstance(embed_limit,bool) or not isinstance(embed_limit,int) or embed_limit < 0 or 0 < embed_limit < 4:
            raise ValueError('embed_max_input_bytes must be zero or an integer >= 4')
        try:
            pricing = Pricing(**fields.pop("pricing", None) or {})
        except (TypeError, ValueError) as exc:
            raise ValueError("bad pricing: %s" % exc) from None
        return cls(pricing=pricing, **fields)


@dataclass(frozen=True)
class ChatResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost: float
    latency_s: float
    finish_reason: Optional[str]


@dataclass(frozen=True)
class UsageRecord:
    started_at: float      # 调用开始的真实时间（Unix 秒）。记忆逻辑不读系统时钟，但账本记的是真实开销
    model: str
    purpose: str           # 流水线环节，例如 extract、query、answer、embed
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost: float
    latency_s: float       # 含重试等待在内的总耗时
    retries: int
    usage_reported: bool   # 服务端没返回 usage 时 token 记 0，报告里要把这类调用单独说明
    tags: Dict[str, str] = field(default_factory=dict)  # 例如 {"question_id": "..."}
    cost_known: Optional[bool] = None  # None：旧账本未记录；历史零费用不能据此认定免费
    pricing: Dict[str, Any] = field(default_factory=dict)


class UsageLedger:
    """线程安全地累计每次模型调用。给了 log_path 就边跑边追加 JSONL，中途中断也不丢账。"""

    SUMMED = ("prompt_tokens", "completion_tokens", "cached_tokens", "cost", "latency_s")

    def __init__(self, log_path: Optional[str] = None):
        self._records: List[UsageRecord] = []
        self._lock = threading.Lock()
        self._log_path = log_path

    @classmethod
    def from_jsonl(cls, path: str) -> UsageLedger:
        """读回之前写下的账本，多次续跑之后用它汇总。"""
        ledger = cls()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ledger._records.append(UsageRecord(**json.loads(line)))
                except (ValueError, TypeError):   # 空行，或进程被杀时写了一半的行
                    continue
        return ledger

    def record(self, rec: UsageRecord) -> None:
        with self._lock:
            self._records.append(rec)
            if self._log_path:
                with open(self._log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")

    def records(self) -> List[UsageRecord]:
        with self._lock:
            return list(self._records)

    def summary(self, by: str = "purpose") -> Dict[str, Dict[str, float]]:
        """按 purpose、model 或某个标签分组汇总；按标签分组写成 "tag:question_id"。"""
        groups: Dict[str, Dict[str, float]] = {}
        for rec in self.records():
            key = rec.tags.get(by[4:], "") if by.startswith("tag:") else str(getattr(rec, by))
            row = groups.setdefault(key, dict.fromkeys(("calls",) + self.SUMMED, 0))
            row["calls"] += 1
            for name in self.SUMMED:
                row[name] += getattr(rec, name)
        return groups

    def totals(self) -> Dict[str, float]:
        rows = self.summary(by="model").values()
        return {name: sum(row[name] for row in rows) for name in ("calls",) + self.SUMMED}


class ChatModel(Protocol):
    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult: ...


class EmbeddingModel(Protocol):
    def embed(self, texts: Sequence[str], *, purpose: str = "embed",
              tags: Optional[Mapping[str, str]] = None) -> List[List[float]]: ...


@dataclass(frozen=True)
class _CallStats:
    started_at: float
    latency_s: float
    retries: int


class OpenAICompatClient:
    """走 OpenAI 兼容接口的对话 + embedding 客户端，带重试和用量记账；同时满足上面两个协议。

    api_key 不为空时优先于 config.api_key_env：可以是 key 本身，也可以是每次调用时取 key 的函数
    （代理模式下借用 agent 发来的 key，key 只在内存里）。
    """

    read_budget_managed = True

    def __init__(self, config: ModelConfig, ledger: Optional[UsageLedger] = None,
                 api_key: Optional[KeySource] = None, *, quota_fail_fast: bool = False):
        self.config = config
        self.ledger = ledger if ledger is not None else UsageLedger()
        self._api_key: Optional[KeySource] = api_key
        self._quota_fail_fast = quota_fail_fast
        self._session_id = uuid.uuid4().hex
        if api_key is None and config.api_key_env:
            self._api_key = os.environ.get(config.api_key_env)
            if not self._api_key:
                raise ModelError(f"environment variable {config.api_key_env} is not set")

    def _key(self) -> Optional[str]:
        return self._api_key() if callable(self._api_key) else self._api_key

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        body: Dict[str, Any] = {"model": self.config.model, "messages": list(messages),
                                "temperature": temperature}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens + self.config.output_token_headroom
        if json_mode and self.config.json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.config.extra_body)
        data, stats = self._post("/chat/completions", body, tags, purpose=purpose)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0].get("message"), dict):
            # 有的服务商过载时返回 200 加一个 error 对象
            raise ModelError("%s returned no choices: %s" % (self.config.base_url, json.dumps(data)[:300]))
        choice = choices[0]
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        # OpenAI 把缓存命中放在 prompt_tokens_details.cached_tokens，DeepSeek 用 prompt_cache_hit_tokens
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                     or usage.get("prompt_cache_hit_tokens") or 0)
        cost = self.config.pricing.cost(prompt, completion, cached)
        self._record(purpose, prompt, completion, cached, cost, stats, bool(usage), tags)
        return ChatResult(
            text=strip_reasoning(choice["message"].get("content") or ""),
            model=self.config.model, prompt_tokens=prompt, completion_tokens=completion,
            cached_tokens=cached, cost=cost, latency_s=stats.latency_s,
            finish_reason=choice.get("finish_reason"))

    def embed(self, texts: Sequence[str], *, purpose: str = "embed",
              tags: Optional[Mapping[str, str]] = None) -> List[List[float]]:
        limit=self.config.embed_max_input_bytes
        flattened=[];groups=[]
        for text in texts:
            pieces=[text]
            if limit and len(text.encode('utf-8'))>limit:
                from .textutil import split_tokens
                pieces=split_tokens(text,limit,count=lambda value:len(value.encode('utf-8')))
            start=len(flattened)
            flattened.extend(pieces)
            groups.append((start,len(flattened),[max(1,len(piece.encode('utf-8'))) for piece in pieces]))
        texts=flattened
        vectors: List[List[float]] = []
        size = max(self.config.embed_batch_size, 1)
        for start in range(0, len(texts), size):
            batch = list(texts[start:start + size])
            body = {"model": self.config.model, "input": batch, **self.config.extra_body}
            data, stats = self._post("/embeddings", body, tags, purpose=purpose)
            if not isinstance(data.get("data"), list):
                raise ModelError("%s returned no embeddings: %s" % (self.config.base_url, json.dumps(data)[:300]))
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            if len(items) != len(batch):
                raise ModelError(f"sent {len(batch)} texts, got {len(items)} embeddings back")
            vectors.extend(item["embedding"] for item in items)
            usage = data.get("usage") or {}
            prompt = int(usage.get("prompt_tokens") or 0)
            self._record(purpose, prompt, 0, 0, self.config.pricing.cost(prompt, 0),
                         stats, bool(usage), tags)
        result=[]
        for start,end,weights in groups:
            if end==start+1:
                result.append(vectors[start])
                continue
            from .vecmath import normalize
            chunks=vectors[start:end]
            dimension=len(chunks[0])
            if not dimension or any(len(v)!=dimension for v in chunks):
                raise ModelError('embedding chunk dimensions differ')
            chunks=[normalize(v) for v in chunks]
            result.append(normalize([sum(weight*v[i] for weight,v in zip(weights,chunks)) for i in range(dimension)]))
        return result

    def _post(self, path: str, body: Mapping[str, Any],
              tags: Optional[Mapping[str, str]] = None, *, purpose: str = "request") -> Tuple[Dict[str, Any], _CallStats]:
        from .read_budget import current_read_budget
        budget = current_read_budget()
        if budget is not None:
            # The request budget owns retries; bypass the legacy write/client loop.
            started_at, t0 = time.time(), time.monotonic()
            before = budget.additional_retries
            data = budget.call(lambda timeout: self._post_once(path, body, tags, timeout, budget),
                               purpose=purpose, timeout_s=self.config.timeout_s)
            return data, _CallStats(started_at, time.monotonic() - t0, budget.additional_retries - before)
        url = self.config.base_url.rstrip("/") + path
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._headers(tags)
        started_at, t0 = time.time(), time.monotonic()
        error = ""
        status: Optional[int] = None
        for attempt in range(self.config.max_retries + 1):
            retry_after: Optional[float] = None
            try:
                request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.config.timeout_s) as resp:
                    raw = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                status = exc.code
                model_error = ModelError(f"{url} returned HTTP {exc.code}: {detail}", status=exc.code)
                if exc.code not in RETRYABLE_STATUS or (self._quota_fail_fast and model_error.quota_or_rate_limit):
                    raise model_error from None
                error = f"HTTP {exc.code}: {detail}"
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
            except (OSError, http.client.HTTPException) as exc:
                status = None
                error = "%s: %s" % (type(exc).__name__, exc)
            else:
                try:
                    data = json.loads(raw)
                except ValueError:
                    raise ModelError(f"{url} returned non-JSON: {raw[:300]!r}") from None
                if not isinstance(data, dict):
                    raise ModelError(f"{url} returned unexpected JSON: {raw[:300]!r}")
                return data, _CallStats(started_at, time.monotonic() - t0, attempt)
            if attempt < self.config.max_retries:
                time.sleep(retry_after if retry_after is not None else _backoff(attempt))
        raise ModelError(f"{url} failed after {self.config.max_retries + 1} attempts: {error}", status=status)

    def _post_once(self, path, body, tags, timeout, budget):
        url = self.config.base_url.rstrip("/") + path
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=self._headers(tags), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = self._read_bounded(response, budget, timeout_cap=timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                detail = self._read_bounded(exc, budget, limit=500, timeout_cap=timeout)
            error = ModelError(f"{url} returned HTTP {exc.code}: {detail}", status=exc.code)
            error.retry_after_s = _retry_after_seconds(exc.headers.get("Retry-After"))
            raise error from None
        except (OSError, http.client.HTTPException) as exc:
            error = ModelError("%s: %s" % (type(exc).__name__, exc))
            error.transport_error = True
            raise error from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise ModelError(f"{url} returned non-JSON: {raw[:300]!r}") from None
        if not isinstance(data, dict):
            raise ModelError(f"{url} returned unexpected JSON: {raw[:300]!r}")
        return data

    @staticmethod
    def _read_bounded(response, budget, limit=None, timeout_cap=120.0):
        # read1 makes progress visible even for a peer slowly sending bytes.
        parts, size = [], 0
        while True:
            budget.check()
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(budget.timeout(timeout_cap))
            chunk = response.read1(min(65536, limit - size) if limit else 65536)
            budget.check()
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
            if limit and size >= limit:
                break
        return b"".join(parts).decode("utf-8", "replace")

    def _headers(self, tags: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        """协议适配器共用会话与客户端标识，各自提供所需鉴权头。"""
        headers = {"Content-Type": "application/json", "User-Agent": "memsys/0.1.0"}
        if urllib.parse.urlsplit(self.config.base_url).hostname == "opencode.ai":
            session = (tags or {}).get("session_id") or (tags or {}).get("question_id") or self._session_id
            headers["x-opencode-session"] = uuid.uuid5(uuid.NAMESPACE_URL, str(session)).hex
        key = self._key()
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def _record(self, purpose: str, prompt: int, completion: int, cached: int, cost: float,
                stats: _CallStats, usage_reported: bool,
                tags: Optional[Mapping[str, str]]) -> None:
        self.ledger.record(UsageRecord(
            started_at=stats.started_at, model=self.config.model, purpose=purpose,
            prompt_tokens=prompt, completion_tokens=completion, cached_tokens=cached,
            cost=cost, latency_s=stats.latency_s, retries=stats.retries,
            usage_reported=usage_reported, tags=dict(tags or {}),
            cost_known=self.config.pricing.known and (usage_reported or
                       self.config.pricing.input == self.config.pricing.output == 0),
            pricing=asdict(self.config.pricing)))


class FunctionChat:
    """把任意函数包成 ChatModel：fn(messages) → 回复文本。接入方用自己的 SDK、自己的 key，记忆系统不管。

    提示词里已经写明"只输出 JSON"，所以不把 json_mode、max_tokens 传给 fn；
    fn 也可以直接返回 ChatResult，带上真实的 token 用量。
    """

    def __init__(self, fn: Callable[[List[Message]], Union[str, ChatResult]], name: str = "function",
                 ledger: Optional[UsageLedger] = None):
        self.fn = fn
        self.name = name
        self.ledger = ledger if ledger is not None else UsageLedger()

    def chat(self, messages: Sequence[Message], *, purpose: str, temperature: float = 0.0,
             max_tokens: Optional[int] = None, json_mode: bool = False,
             tags: Optional[Mapping[str, str]] = None) -> ChatResult:
        started_at, t0 = time.time(), time.monotonic()
        try:
            reply = self.fn([dict(m) for m in messages])
        except ModelError:
            raise
        except Exception as exc:   # 接入方函数里的任何错误都按模型调用失败处理，上层有统一的兜底
            raise ModelError("%s failed: %s: %s" % (self.name, type(exc).__name__, exc)) from exc
        latency = time.monotonic() - t0
        if isinstance(reply, ChatResult):
            result = reply
        else:
            result = ChatResult(text=strip_reasoning(str(reply or "")), model=self.name, prompt_tokens=0,
                                completion_tokens=0, cached_tokens=0, cost=0.0, latency_s=latency,
                                finish_reason="stop")
        self.ledger.record(UsageRecord(
            started_at=started_at, model=result.model, purpose=purpose, prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens, cached_tokens=result.cached_tokens, cost=result.cost,
            latency_s=latency, retries=0, usage_reported=isinstance(reply, ChatResult), tags=dict(tags or {}),
            cost_known=True if isinstance(reply, ChatResult) and reply.cost > 0 else None))
        return result


class FunctionEmbedder:
    """把任意函数包成 EmbeddingModel：fn(texts) → 向量列表。"""

    def __init__(self, fn: Callable[[List[str]], Sequence[Sequence[float]]], name: str = "function"):
        self.fn = fn
        self.name = name

    def embed(self, texts: Sequence[str], *, purpose: str = "embed",
              tags: Optional[Mapping[str, str]] = None) -> List[List[float]]:
        try:
            vectors = [list(map(float, v)) for v in self.fn(list(texts))]
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError("%s failed: %s: %s" % (self.name, type(exc).__name__, exc)) from exc
        if len(vectors) != len(texts):
            raise ModelError(f"sent {len(texts)} texts, got {len(vectors)} embeddings back")
        return vectors


def chat_json(model: ChatModel, messages: Sequence[Message], *, purpose: str, **kwargs: Any) -> Any:
    """让模型返回 JSON 并解析，解析失败抛 BadJSONError，由调用方决定重试还是跳过。

    注意：OpenAI 的 json_object 模式要求消息里出现 "JSON" 字样，否则直接返回 400，
    提示词里要写明"以 JSON 格式输出"。
    """
    result = model.chat(messages, purpose=purpose, json_mode=True, **kwargs)
    if result.finish_reason in ("length", "incomplete"):
        raise IncompleteJSONError(result.finish_reason)
    if result.finish_reason in ("content_filter", "refusal"):
        raise ModelError("JSON response was not completed: " + result.finish_reason)
    return parse_json(result.text)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def parse_json(text: str) -> Any:
    """解析回复里的 JSON，容忍 ```json 代码块和前后多余的文字。"""
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except ValueError:
                continue
    raise BadJSONError(text)


def strip_reasoning(text: str) -> str:
    """去掉部分推理模型部署放在回复开头的 <think>…</think>，免得思考过程混进答案和 hypothesis。"""
    return _THINK.sub("", text, count=1)


def _backoff(attempt: int) -> float:
    """指数退避加随机抖动：大约 1、2、4、8 秒，封顶 30 秒。"""
    return min(2.0 ** attempt, 30.0) * random.uniform(0.75, 1.25)


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """只认秒数写法的 Retry-After；HTTP 日期写法少见，交给指数退避。"""
    if value is None:
        return None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        return None
