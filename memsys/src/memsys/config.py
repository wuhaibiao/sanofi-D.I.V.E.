"""配置：模型（谁来做什么）和记忆策略的所有可调参数。

配置文件是 JSON，结构见仓库根目录的 config.example.json：

    {"models": {名字: ModelConfig 字段...},
     "roles": {"chat": 名字, "memory": 名字, "embedding": 名字, "judge": 名字, ...},
     "memory": {MemorySettings 字段...}}

chat 由宿主负责回答，memory 负责新增记忆的单次抽取；extract 可以单独指定。
默认检索不调用生成式模型；query / rerank 仅在显式启用可选后端时使用。
所有角色都可以不写：没写的由 discover.py 去找
（宿主借给的模型、环境变量里的 key、本机 Ollama），找不到就降级运行，见 Models.tier。
"""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Mapping, Optional

from .qdrant import QdrantSettings
from . import prompts
from .llm import ChatModel, EmbeddingModel, ModelConfig, OpenAICompatClient, UsageLedger


@dataclass
class MemorySettings:
    budget_mode: str = "utf8"           # utf8 按字节计数；tokenizer 按 token；estimate 为旧估算消融
    budget_tokenizer_path: str = ""
    answer_input_budget_tokens: int = 131072  # 完整输入预算；utf8 模式单位为字节
    memory_input_budget_tokens: int = 16384  # 可选查询/LLM 重排等预算；不限制整批记忆抽取
    max_storage_bytes: Optional[int] = 268435456
    aggregate_candidate_limit: int = 5000
    aggregate_contribution_limit: int = 5000
    aggregate_max_pages: int = 128
    aggregate_page_items: int = 20
    # ---- 写入
    gate_threshold: float = 0.1          # 显著性低于它的轮次（寒暄等）不进索引
    extract_max_tokens: int = 65536      # 整批抽取的初始输出预算
    extract_max_tokens_limit: int = 131072  # 触顶后原输入扩大重试的上限，最多额外两次
    extract_context_memories: int = 10   # 同一次抽取提示里携带的相关旧记忆上限
    embed_chars: int = 2000              # 分段向量的单段字符上限；关闭分段时是旧前缀长度
    episode_chunk_vectors: bool = True  # 原文按说话人分段编码，覆盖整个轮次
    embed_chunk_tokens: int = 384        # 单个原文向量输入（含角色标签）的估算 token 上限
    chunk_strategy: str = "legacy"       # 保留旧配置字段；仅支持原文按角色连续分段
    embedding_model_revision: str = "unknown"  # 服务模型的不可变身份；未知时明确记录而非冒充已固定
    resolve_similarity: float = 0.75     # 旧配置兼容字段；已停用
    resolve_max_candidates: int = 5      # 旧配置兼容字段；已停用
    tentative_threshold: float = 0.6     # 低于它在记忆块里标 tentative；不裁决取代关系
    flush_rounds: int = 12               # 对话模式下累计这么多轮未整理就先整理一次
    confirm_conflicts: bool = False      # 旧配置兼容字段；新增流程不生成待确认冲突
    # ---- 读取
    recall_mode: str = "facts"         # raw_chunks: 原文分块存取，不调用生成式事实抽取
    raw_chunk_chars: int = 2000
    raw_chunk_overlap_chars: int = 200
    max_raw_chunks: int = 20
    read_timeout_s: float = 600.0
    read_retries_per_call: int = 1
    read_retries_total: int = 2
    read_retry_wait_s: float = 30.0
    query_analysis: bool = False         # 默认直接检索；仅显式启用时调用查询规划模型
    query_expansion: bool = False        # 查询分析时多要同义词和换个说法：没有语义 embedding 时靠它补召回
    max_queries: int = 4
    dense_k: int = 30
    sparse_k: int = 30
    rrf_k: int = 60
    expand_weight: float = 0.5           # 事实命中时，给它的来源原文加多少分（"事实作键、原文作值"）
    llm_rerank: bool = True              # 让 memory 模型从检索候选里挑出真正需要的条目（消融时可关）
    rerank_backend: str = "bge"         # 本地重排为默认；llm 对照，fusion 离线/降级
    rerank_url: str = ""
    rerank_api_key_env: str = "RERANK_API_KEY"
    rerank_timeout_s: float = 60.0
    worker_count: int = 4
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_revision: str = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
    rerank_tokenizer_revision: str = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
    rerank_device: str = "cpu"
    rerank_dtype: str = "float32"
    rerank_batch_size: int = 4
    rerank_max_tokens: int = 512
    rerank_overlap_tokens: int = 32
    rerank_strict: bool = False          # 质量对照必须打开；运行失败可记录后退回 fusion
    query_policy: str = "off"          # always / on_risk / off；默认无生成式查询规划
    query_low_confidence: float = 0.65
    rerank_candidates: int = 60          # 交给重排的候选上限：输入规模由它决定，与历史长度无关
    min_similarity: float = 0.25         # 向量相似度低于它、又没有关键词命中的条目视为不相关
    fallback_min_candidates: int = 3     # 相关条目少于这个数时，去已归档的记忆里兜底再找
    time_boost: float = 1.5
    time_penalty: float = 0.7
    time_slack_days: int = 2
    superseded_weight: float = 0.5       # 问"现在"时，已被取代的事实降权
    confidence_weight: float = 0.3       # 事实按置信度降权的力度：分数 ×(1 - w×(1 - 置信度))
    context_budget_tokens: int = 32000   # 工作记忆及取证分页预算；utf8 模式下单位为字节
    max_history_share: float = 1.0       # 工作记忆最多占全部历史的多少：1.0 表示送进去的记忆不超过历史本身；0 表示不限
    min_context_tokens: int = 1500       # 短历史仍须容纳事实日期、关系和来源等呈现开销
    fact_budget_share: float = 0.35
    fact_budget_share_all: float = 0.6   # 需要全部召回时事实可占的预算比例：事实紧凑，聚合题要的是广度
    selection_policy: str = "ranked"   # ranked 基线；coverage 按子查询和事实来源选互补证据
    max_facts: int = 30
    max_facts_all: int = 60              # 需要全部召回（计数、求和、列举）时放宽
    relations_enabled: bool = True      # 事实上的实体关系索引；不会读取历史原文
    relation_hops: int = 2
    relation_seed_facts: int = 6
    relation_max_nodes: int = 32
    relation_max_edges: int = 96
    relation_max_candidates: int = 32
    answer_fact_limit: int = 128         # 宿主首次回答前覆盖补取的事实条数上限
    max_episodes: int = 8
    episode_max_tokens: int = 1800      # 单条原文摘录上限，仍受剩余总预算限制；utf8 时为字节
    query_focused_excerpts: bool = True # 长原文优先保留与查询匹配的片段，而非只截取开头
    chat_window_turns: int = 12          # 最近完整对话轮上限（不是消息条数），另受窗口预算限制
    chat_window_tokens: int = 12000
    answer_max_tokens: int = 8192
    answer_max_tokens_limit: int = 32768
    aggregation_validation: bool = False # 回答中的结构化计算凭据由程序核验
    semantic_validation: bool = True    # 操作、跨页事件/累计状态和回答相关性检查
    complete_episode_context: bool = False  # 完整原文能放下时直接综合；否则沿用分页取证
    complete_context_skip_rerank: bool = False  # 只有完整来源及输入预算均通过才省去排序
    complete_context_direct_query: bool = False  # 全部可检索原文及来源均能完整放入时直接读取
    semantic_operation_validation: Optional[bool] = None  # None 沿用旧开关
    cross_page_semantic_validation: Optional[bool] = None
    answer_review_policy: str = "off"  # legacy 沿用旧开关；always / on_risk / off 独立控制复核
    # ---- 遗忘
    half_life_days: float = 90.0
    archive_after_days: int = 180        # 至少这么久没被用到，才可能被降级归档
    archive_threshold: float = 0.2
    superseded_archive_days: int = 365   # 被取代超过这么久的旧事实归档（仍可兜底检索）
    max_active_facts: Optional[int] = 5000
    max_active_episodes: Optional[int] = 2000

    # 影响"记忆库里存了什么"的参数：写入路径和遗忘策略（遗忘会在回放过程中改变条目状态）
    WRITE_FIELDS = ("budget_mode", "budget_tokenizer_path", "gate_threshold", "extract_max_tokens", "extract_max_tokens_limit",
                    "embedding_model_revision", "extract_context_memories",
                    "tentative_threshold", "relations_enabled",
                    "half_life_days", "archive_after_days",
                    "archive_threshold", "superseded_archive_days", "max_active_facts", "max_active_episodes")

    # 取值必须落在 [0, 1] 的比例类参数
    SHARE_FIELDS = ("fact_budget_share", "fact_budget_share_all", "min_similarity", "resolve_similarity",
                    "gate_threshold", "archive_threshold", "superseded_weight", "tentative_threshold",
                    "confidence_weight", "max_history_share")

    INDEX_FIELDS = ("embed_chars", "episode_chunk_vectors", "embed_chunk_tokens", "chunk_strategy",
                    "embedding_model_revision")
    RETIRED_CHUNK_FIELDS = ("embedding_tokenizer_model", "embedding_tokenizer_revision", "embedding_tokenizer_path",
                           "chunk_overlap_tokens", "embed_question_tokens", "parent_max_tokens", "parent_neighbor_rounds",
                           "semantic_chunking", "semantic_boundary_threshold", "semantic_min_tokens", "semantic_trigger_tokens")
    RETIRED_EXTRACT_FIELDS = ("extract_chunk_tokens", "extract_full_rounds", "extract_assistant_chars", "extract_context_tokens")

    @property
    def operation_semantics(self) -> bool:
        return self.semantic_validation if self.semantic_operation_validation is None else self.semantic_operation_validation

    @property
    def include_sources(self) -> bool:
        return self.recall_mode in ("facts_and_sources", "raw_chunks")

    @property
    def page_semantics(self) -> bool:
        return self.semantic_validation if self.cross_page_semantic_validation is None else self.cross_page_semantic_validation

    @property
    def review_policy(self) -> str:
        if self.answer_review_policy == "legacy":
            return "always" if self.semantic_validation else "off"
        return self.answer_review_policy

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MemorySettings:
        data = dict(data)
        retired_extract = [name for name in cls.RETIRED_EXTRACT_FIELDS if name in data]
        for name in retired_extract:
            data.pop(name)
        if retired_extract:
            warnings.warn("retired extraction limits ignored; messages are processed in one batch: %s" %
                          ", ".join(retired_extract), FutureWarning, stacklevel=2)
        if "semantic_chunking" in data and data["semantic_chunking"] is not False:
            raise ValueError("semantic chunking was removed; use legacy role chunks")
        retired = [name for name in cls.RETIRED_CHUNK_FIELDS if name in data]
        if retired:
            for name in retired:
                data.pop(name)
            warnings.warn("retired chunk settings ignored; remove: %s" % ", ".join(retired),
                          FutureWarning, stacklevel=2)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise TypeError("unknown memory settings: %s" % ", ".join(sorted(unknown)))
        settings = cls(**data)
        settings.validate()
        return settings

    def validate(self) -> None:
        """配置文件写错（类型不对、比例超出 0~1、预算为负）时尽早报清楚，而不是跑到一半出怪结果。"""
        defaults = MemorySettings()
        for name in ("read_timeout_s", "read_retry_wait_s"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or value < 0 or name == "read_timeout_s" and value == 0):
                raise ValueError("memory.%s must be finite and %s" % (name, "positive" if name == "read_timeout_s" else "non-negative"))
        choices = {"chunk_strategy": ("legacy",), "rerank_backend": ("llm", "bge", "fusion", "remote"),
                   "rerank_device": ("auto", "cpu", "mps", "cuda"), "rerank_dtype": ("float32", "float16", "bfloat16"),
                   "query_policy": ("always", "on_risk", "off"),
                   "selection_policy": ("ranked", "coverage"),
                   "recall_mode": ("facts", "facts_and_sources", "raw_chunks"),
                   "answer_review_policy": ("legacy", "always", "on_risk", "off")}
        for name, allowed in choices.items():
            if getattr(self, name) not in allowed:
                raise ValueError("memory.%s must be one of %s" % (name, ", ".join(allowed)))
        for name in ("semantic_operation_validation", "cross_page_semantic_validation"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), bool):
                raise ValueError("memory.%s must be null, true or false" % name)
        if self.budget_mode not in ("utf8", "estimate", "tokenizer"):
            raise ValueError("budget_mode must be utf8, estimate or tokenizer")
        if not isinstance(self.budget_tokenizer_path, str):
            raise ValueError("budget_tokenizer_path must be a string")
        if self.budget_mode == "tokenizer" and not self.budget_tokenizer_path:
            raise ValueError("tokenizer budget requires budget_tokenizer_path")
        for f in fields(self):
            value, default = getattr(self, f.name), getattr(defaults, f.name)
            if f.name in ("max_active_facts", "max_active_episodes", "max_storage_bytes"):
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    raise ValueError("memory.%s must be null or a non-negative integer, got %r" % (f.name, value))
                continue
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ValueError("memory.%s must be true or false, got %r" % (f.name, value))
            elif isinstance(default, int):
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError("memory.%s must be a non-negative integer, got %r" % (f.name, value))
            elif isinstance(default, float):
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                    raise ValueError("memory.%s must be a non-negative number, got %r" % (f.name, value))
            elif isinstance(default, str) and not isinstance(value, str):
                raise ValueError("memory.%s must be a string" % f.name)
        for name in self.SHARE_FIELDS:
            if getattr(self, name) > 1:
                raise ValueError("memory.%s must be between 0 and 1, got %r" % (name, getattr(self, name)))
        if self.raw_chunk_chars < 64 or self.raw_chunk_overlap_chars >= self.raw_chunk_chars // 2 or self.max_raw_chunks < 1:
            raise ValueError("raw chunk size must be >= 64, overlap < half its size, and max_raw_chunks >= 1")
        if self.recall_mode == "raw_chunks" and not self.episode_chunk_vectors:
            raise ValueError("raw_chunks requires episode_chunk_vectors")
        if self.worker_count < 1 or self.rerank_timeout_s <= 0 or not math.isfinite(self.rerank_timeout_s):
            raise ValueError("worker_count and rerank_timeout_s must be positive")
        if self.rerank_backend == "remote" and not self.rerank_url:
            raise ValueError("remote rerank requires rerank_url")
        if self.half_life_days <= 0:
            raise ValueError("memory.half_life_days must be positive")
        if self.max_storage_bytes == 0:
            raise ValueError("memory.max_storage_bytes must be positive or null")
        if min(self.aggregate_candidate_limit, self.aggregate_contribution_limit, self.aggregate_max_pages, self.aggregate_page_items,
               self.answer_input_budget_tokens, self.memory_input_budget_tokens) <= 0:
            raise ValueError("aggregate limits and input budgets must be positive")
        for purpose in ('answer','extract'):
            initial=getattr(self,purpose+'_max_tokens')
            ceiling=getattr(self,purpose+'_max_tokens_limit')
            if initial < 1 or ceiling < initial:
                raise ValueError('memory.%s output limits require 0 < max_tokens <= max_tokens_limit' % purpose)
        if self.episode_chunk_vectors and (self.embed_chars < 1 or self.embed_chunk_tokens < 16):
            raise ValueError("chunk embeddings require embed_chars >= 1 and embed_chunk_tokens >= 16")
        if not 0 <= self.query_low_confidence <= 1:
            raise ValueError("query_low_confidence must be between 0 and 1")
        if self.rerank_batch_size < 1 or self.rerank_max_tokens < 16 or self.rerank_overlap_tokens >= self.rerank_max_tokens // 2:
            raise ValueError("invalid reranker batch size, pair budget or overlap")
        if not 0 <= self.relation_hops <= 2 or min(self.relation_seed_facts, self.relation_max_nodes,
                self.relation_max_edges, self.relation_max_candidates, self.answer_fact_limit) < 1:
            raise ValueError("relations require 0..2 hops and positive bounds")

    def write_fingerprint(self) -> str:
        """写入相关参数和提示词的指纹；评测时据此判断缓存的记忆库能不能复用。"""
        values = {name: getattr(self, name) for name in self.WRITE_FIELDS}
        from .budget import budget_counter
        values["budget_counter"] = budget_counter(self).manifest()
        values["prompts"] = [prompts.EXTRACT_SYSTEM, prompts.EXTRACT_USER]
        values["write_policy"] = "single-batch-additive-grounded-relations-v7"
        return hashlib.sha1(json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def index_fingerprint(self, embedding_name: str, *, legacy_counter=None) -> str:
        values = {name: getattr(self, name) for name in self.INDEX_FIELDS}
        values["embedding_identity"] = embedding_name
        values["index_format"] = 2
        from pathlib import Path
        directory = Path(__file__).parent
        values["index_code_sha256"] = hashlib.sha256((directory / "indexing.py").read_bytes()).hexdigest()
        if self.recall_mode == "raw_chunks":
            values.update(raw_chunks_format=1, raw_chunk_chars=self.raw_chunk_chars,
                          raw_chunk_overlap_chars=self.raw_chunk_overlap_chars,
                          raw_chunks_code_sha256=hashlib.sha256((directory / "raw_chunks.py").read_bytes()).hexdigest())
        from .fact_content import index_signature
        values.update(index_signature())
        from .budget import budget_counter
        values["counter"] = legacy_counter or budget_counter(self).manifest()
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def read_fingerprint(self) -> str:
        values = {key: value for key, value in self.to_dict().items() if key not in self.WRITE_FIELDS and key not in self.INDEX_FIELDS}
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


PURPOSES = ("extract", "query", "rerank")
ROLES = ("chat", "memory", "embedding", "judge") + PURPOSES
TIERS = {"L0": "没有记忆模型：只存原文轮次，关键词检索，不抽事实、不重排",
         "L1": "有记忆模型、没有语义 embedding：抽取与新增记忆，向量只有词面相似度",
         "L2": "记忆抽取 + 语义 embedding：新增、去重与检索"}


@dataclass
class Models:
    chat: Optional[ChatModel]       # 生成回答；外部 agent 自己回答时可以没有
    memory: Optional[ChatModel]     # 单次记忆抽取；可选查询/LLM 重排须显式启用
    embedder: EmbeddingModel        # 没有 embedding 服务时用本地哈希向量
    embedding_name: str             # 写进记忆库，换 embedding 模型后要 reindex
    names: Dict[str, str] = field(default_factory=dict)
    by_purpose: Dict[str, ChatModel] = field(default_factory=dict)   # 按用途单独指定的模型，优先于 memory
    semantic_embedding: bool = True  # False：本地哈希向量，只有词面相似度
    sources: Dict[str, str] = field(default_factory=dict)            # 角色 → 模型从哪来（config、host、env、ollama…）

    def for_purpose(self, purpose: str) -> Optional[ChatModel]:
        return self.by_purpose.get(purpose) or self.memory

    @property
    def tier(self) -> str:
        if self.for_purpose("extract") is None:
            return "L0"
        return "L2" if self.semantic_embedding else "L1"


@dataclass
class AppConfig:
    models: Dict[str, ModelConfig]
    roles: Dict[str, str]
    memory: MemorySettings
    fallbacks: Dict[str, List[str]] = field(default_factory=dict)
    storage: QdrantSettings = field(default_factory=QdrantSettings.environment)

    @classmethod
    def load(cls, path: str) -> AppConfig:
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AppConfig:
        """以下划线开头的键视为注释，方便在 JSON 里写说明。"""
        def plain(mapping: Mapping[str, Any]) -> Dict[str, Any]:
            return {k: v for k, v in mapping.items() if not k.startswith("_")}

        models = {}
        for name, spec in plain(data.get("models", {})).items():
            try:
                models[name] = ModelConfig.from_dict(plain(spec))
            except (TypeError, ValueError) as exc:
                raise ValueError("config.models.%s: %s" % (name, exc)) from None
        roles = plain(data.get("roles", {}))
        if "resolve" in roles:
            roles.pop("resolve")
            warnings.warn("roles.resolve is retired and ignored; writes are additive", FutureWarning, stacklevel=2)
        for role, name in roles.items():
            if role not in ROLES:
                raise ValueError("config.roles.%s is not a role (roles: %s)" % (role, ", ".join(ROLES)))
            if name not in models:
                raise ValueError(f"config.roles.{role} refers to unknown model {name!r}")
        if not isinstance(data.get("fallbacks", {}), Mapping):
            raise ValueError("config.fallbacks must be an object of model-name lists")
        fallbacks = plain(data.get("fallbacks", {}))
        for name, candidates in fallbacks.items():
            if name not in models:
                raise ValueError(f"config.fallbacks refers to unknown model {name!r}")
            if not isinstance(candidates, list) or not all(isinstance(item, str) for item in candidates):
                raise ValueError(f"config.fallbacks.{name} must be a list of model names")
            if name in candidates or len(set(candidates)) != len(candidates):
                raise ValueError(f"config.fallbacks.{name} contains a duplicate or the primary model")
            for candidate in candidates:
                if candidate not in models:
                    raise ValueError(f"config.fallbacks.{name} refers to unknown model {candidate!r}")
            if roles.get("embedding") in [name] + candidates or any(models[item].base_url == "local:hash"
                                                                   for item in [name] + candidates):
                raise ValueError("quota fallbacks are for chat models; embedding must keep the same vector space")
        try:
            memory = MemorySettings.from_dict(plain(data.get("memory", {})))
        except TypeError as exc:
            raise ValueError("config.memory: %s" % exc) from None
        return cls(models=models, roles=roles, memory=memory, fallbacks=fallbacks,
                   storage=QdrantSettings.from_dict(plain(data.get("storage", {}))) if data.get("storage") else QdrantSettings.environment())

    def client(self, role_or_name: str, ledger: UsageLedger, api_key: Optional[Any] = None) -> ChatModel:
        name = self.roles.get(role_or_name, role_or_name)
        if self.fallbacks.get(name):
            from .fallback import QuotaFallbackChat
            names = [name] + self.fallbacks[name]
            return QuotaFallbackChat([make_client(self.models[item], ledger, api_key, quota_fail_fast=True)
                                      for item in names], names)
        return make_client(self.models[name], ledger, api_key)

    def build_models(self, ledger: UsageLedger, **discover_kwargs: Any) -> Models:
        """配置里写了的角色照配置来，没写的交给 discover 去找。"""
        from .discover import resolve_models
        return resolve_models(self, ledger, **discover_kwargs)


def make_client(config: ModelConfig, ledger: UsageLedger, api_key: Optional[Any] = None, *,
                quota_fail_fast: bool = False) -> Any:
    """按 ModelConfig.api 建对话/embedding 客户端；本地哈希向量另见 discover.hash_embedder。"""
    if config.api == "responses":
        from .protocols import ResponsesClient
        return ResponsesClient(config, ledger, api_key=api_key, quota_fail_fast=quota_fail_fast)
    if config.api == "anthropic":
        if urlsplit(config.base_url).hostname == "opencode.ai":
            from .protocols import AnthropicMessagesClient
            return AnthropicMessagesClient(config, ledger, api_key=api_key, quota_fail_fast=quota_fail_fast)
        from .anthropic_client import AnthropicClient
        return AnthropicClient(config, ledger, api_key=api_key)
    return OpenAICompatClient(config, ledger, api_key=api_key, quota_fail_fast=quota_fail_fast)
