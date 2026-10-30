"""找模型：记忆系统不自带模型，也不绑定模型，用接入方手里有的。

每个角色按下面的顺序找，找到就停：

1. 配置文件里写明的（config.json 的 roles）
2. 宿主借给的（HostModels）：代理模式借上游模型和 agent 发来的 key，MCP 模式借客户端的模型，
   Python 调用方直接传函数进来
3. 环境变量：先看 MEMSYS_LLM_* / MEMSYS_EMBED_*，再看常见服务商的 key（OPENAI_API_KEY 等）
4. 本机 Ollama（OLLAMA_HOST，默认 localhost:11434）
5. 都没有：记忆模型留空（降级到 L0），embedding 用本地哈希向量

找的结果写进 Models.sources，`memsys doctor` 会打印出来。这里不发真实的模型请求，
只有 Ollama 会被探测一下（0.5 秒超时）。
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .config import PURPOSES, AppConfig, Models, make_client
from .fake import HashEmbedder
from .llm import ChatModel, EmbeddingModel, ModelConfig, UsageLedger

HASH_DIM = 512
HASH_NAME = "hash-%d@local:hash" % HASH_DIM


@dataclass(frozen=True)
class Provider:
    key_env: str
    base_url: str
    chat_model: Optional[str]
    embed_model: Optional[str] = None
    api: str = "openai"
    base_url_env: Optional[str] = None


# 只放接口和默认模型名都比较稳定的服务商。默认模型不合适就用 MEMSYS_LLM_MODEL / MEMSYS_EMBED_MODEL 覆盖
PROVIDERS = (
    Provider("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini", "text-embedding-3-small",
             base_url_env="OPENAI_BASE_URL"),
    Provider("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    Provider("DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
             "text-embedding-v3"),
    Provider("SILICONFLOW_API_KEY", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct", "BAAI/bge-m3"),
    Provider("ANTHROPIC_API_KEY", "https://api.anthropic.com", "claude-opus-5-5", None, api="anthropic",
             base_url_env="ANTHROPIC_BASE_URL"),
)
_EMBED_HINTS = ("embed", "bge", "e5", "gte", "nomic", "minilm")


@dataclass
class HostModels:
    """接入层借给记忆系统的模型。names 是显示用的名字，会写进 trace 和评测配置。"""
    chat: Optional[ChatModel] = None
    memory: Optional[ChatModel] = None
    embedder: Optional[EmbeddingModel] = None
    embedding_name: Optional[str] = None
    names: Dict[str, str] = field(default_factory=dict)
    source: str = "host"


@dataclass
class Found:
    config: ModelConfig
    source: str
    key: Optional[str] = None     # 从传进来的环境里读到的 key；直接交给客户端，不再回头读 os.environ


def hash_embedder() -> HashEmbedder:
    return HashEmbedder(dim=HASH_DIM)


def _env_llm(env: Mapping[str, str]) -> Optional[Found]:
    base = env.get("MEMSYS_LLM_BASE_URL")
    if base:
        model = env.get("MEMSYS_LLM_MODEL")
        if not model:
            return None
        api = env.get("MEMSYS_LLM_API", "anthropic" if "anthropic.com" in base else "openai")
        key_env = "MEMSYS_LLM_API_KEY" if env.get("MEMSYS_LLM_API_KEY") else None
        return Found(ModelConfig(model=model, base_url=base, api=api, api_key_env=key_env), "env:MEMSYS_LLM_*",
                     env.get("MEMSYS_LLM_API_KEY"))
    for p in PROVIDERS:
        if env.get(p.key_env) and p.chat_model:
            base_url = (env.get(p.base_url_env) if p.base_url_env else None) or p.base_url
            model = env.get("MEMSYS_LLM_MODEL") or p.chat_model
            return Found(ModelConfig(model=model, base_url=base_url, api=p.api, api_key_env=p.key_env),
                         "env:" + p.key_env, env[p.key_env])
    return None


def _env_embed(env: Mapping[str, str]) -> Optional[Found]:
    base = env.get("MEMSYS_EMBED_BASE_URL")
    if base:
        model = env.get("MEMSYS_EMBED_MODEL")
        if not model:
            return None
        key_env = "MEMSYS_EMBED_API_KEY" if env.get("MEMSYS_EMBED_API_KEY") else None
        return Found(ModelConfig(model=model, base_url=base, api_key_env=key_env), "env:MEMSYS_EMBED_*",
                     env.get("MEMSYS_EMBED_API_KEY"))
    for p in PROVIDERS:
        if env.get(p.key_env) and p.embed_model:
            base_url = (env.get(p.base_url_env) if p.base_url_env else None) or p.base_url
            model = env.get("MEMSYS_EMBED_MODEL") or p.embed_model
            return Found(ModelConfig(model=model, base_url=base_url, api_key_env=p.key_env), "env:" + p.key_env,
                         env[p.key_env])
    return None


def ollama_models(env: Mapping[str, str], timeout: float = 0.5) -> Tuple[Optional[str], List[str]]:
    """探测本机 Ollama：返回 (OpenAI 兼容接口地址, 已下载的模型名)；没在运行就返回 (None, [])。"""
    host = env.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None, []
    names = [str(m.get("name") or m.get("model")) for m in data.get("models", []) if isinstance(m, dict)]
    return host + "/v1", [n for n in names if n and n != "None"]


def _is_embedding(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in _EMBED_HINTS)


class _Ollama:
    def __init__(self, env: Mapping[str, str], probe: bool):
        self.env, self.probe = env, probe
        self._result: Optional[Tuple[Optional[str], List[str]]] = None

    def _get(self) -> Tuple[Optional[str], List[str]]:
        if self._result is None:
            self._result = ollama_models(self.env) if self.probe else (None, [])
        return self._result

    def llm(self) -> Optional[Found]:
        base, names = self._get()
        chat = [n for n in names if not _is_embedding(n)]
        if not base or not chat:
            return None
        wanted = self.env.get("MEMSYS_LLM_MODEL")
        model = wanted if wanted in chat else chat[0]
        return Found(ModelConfig(model=model, base_url=base), "ollama")

    def embed(self) -> Optional[Found]:
        base, names = self._get()
        embed = [n for n in names if _is_embedding(n)]
        if not base or not embed:
            return None
        preferred = [n for n in embed if n.split(":")[0] == "bge-m3"]
        return Found(ModelConfig(model=(preferred or embed)[0], base_url=base, embed_batch_size=32), "ollama")


def resolve_models(config: Optional[AppConfig], ledger: UsageLedger, host: Optional[HostModels] = None,
                   env: Optional[Mapping[str, str]] = None, probe: bool = True) -> Models:
    """按模块说明里的顺序给每个角色找模型。"""
    env = os.environ if env is None else env
    roles = dict(config.roles) if config else {}
    ollama = _Ollama(env, probe)
    names: Dict[str, str] = {}
    sources: Dict[str, str] = {}
    configured_clients: Dict[str, Any] = {}

    def from_config(role: str) -> Optional[Any]:
        name = roles.get(role)
        if not name:
            return None
        names[role], sources[role] = name, "config"
        if name not in configured_clients:
            configured_clients[name] = config.client(name, ledger)
        return configured_clients[name]

    def discovered(role: str) -> Optional[Any]:
        found = _env_llm(env) or ollama.llm()
        if found is None:
            names[role], sources[role] = "", "none"
            return None
        names[role], sources[role] = found.config.model, found.source
        return make_client(found.config, ledger, api_key=found.key)

    def from_host(role: str, model: Optional[ChatModel]) -> Optional[ChatModel]:
        if host is None or model is None:
            return None
        names[role] = host.names.get(role, host.source)
        sources[role] = host.source
        return model

    memory = from_config("memory") or from_host("memory", host.memory if host else None) or discovered("memory")
    chat = (from_config("chat") or from_host("chat", host.chat if host else None)
            or from_host("chat", host.memory if host else None) or discovered("chat"))
    by_purpose = {}
    for purpose in PURPOSES:
        model = from_config(purpose)
        if model is not None:
            by_purpose[purpose] = model
    if config and roles.get("judge"):
        names["judge"] = roles["judge"]

    # embedding
    semantic = True
    embedding_cfg = config.models.get(roles["embedding"]) if config and roles.get("embedding") else None
    if embedding_cfg is not None:
        sources["embedding"] = "config"
        names["embedding"] = roles["embedding"]
        if embedding_cfg.base_url == "local:hash":
            # 本地特征哈希向量：不联网、不花钱，只有词面相似度。没有 embedding 服务时的兜底
            embedder: EmbeddingModel = HashEmbedder(dim=int(embedding_cfg.extra_body.get("dim", HASH_DIM)))
            semantic = False
        else:
            embedder = make_client(embedding_cfg, ledger)
        embedding_name = "%s@%s" % (embedding_cfg.model, embedding_cfg.base_url)
    elif host is not None and host.embedder is not None:
        embedder, embedding_name = host.embedder, host.embedding_name or "%s-embedding" % host.source
        names["embedding"], sources["embedding"] = host.names.get("embedding", embedding_name), host.source
    else:
        found = _env_embed(env) or ollama.embed()
        if found is not None:
            embedder = make_client(found.config, ledger, api_key=found.key)
            embedding_name = "%s@%s" % (found.config.model, found.config.base_url)
            names["embedding"], sources["embedding"] = found.config.model, found.source
        else:
            embedder, embedding_name, semantic = hash_embedder(), HASH_NAME, False
            names["embedding"], sources["embedding"] = "hash-%d" % HASH_DIM, "builtin"
    return Models(chat=chat, memory=memory, embedder=embedder, embedding_name=embedding_name, names=names,
                  by_purpose=by_purpose, semantic_embedding=semantic, sources=sources)


def describe(models: Models) -> List[str]:
    """给 doctor / init / 启动日志用的多行说明。"""
    from .config import TIERS
    lines = ["档位：%s（%s）" % (models.tier, TIERS[models.tier])]
    for role in ("memory",) + PURPOSES + ("chat", "embedding"):
        if role in PURPOSES and role not in models.by_purpose:
            continue
        name = models.names.get(role) or "—"
        lines.append("  %-9s %-28s 来源：%s" % (role, name, models.sources.get(role, "none")))
        model = models.embedder if role == "embedding" else (
            models.chat if role == "chat" else models.for_purpose(role))
        if hasattr(model, "candidate_names"):
            lines.append("    额度候选：%s；当前：%s" % (" → ".join(model.candidate_names), model.active_name))
    if models.tier == "L0":
        lines.append("  没有记忆模型：原文照样入库，之后有了模型运行 memsys rebuild 补抽事实。")
    if not models.semantic_embedding:
        lines.append("  没有语义 embedding：向量检索只有词面相似度；装上后运行 memsys reindex 重算向量。")
    if models.chat is None:
        lines.append("  没有回答模型：recall / 代理 / MCP 可以用（由外部 agent 回答），内置的 chat 和评测不能用。")
    return lines


def discovered_configs(env: Optional[Mapping[str, str]] = None, probe: bool = True) -> Dict[str, Found]:
    """不看配置文件、只看环境和本机时能找到的模型；memsys init 用它写出一份配置。"""
    env = os.environ if env is None else env
    ollama = _Ollama(env, probe)
    found: Dict[str, Found] = {}
    llm = _env_llm(env) or ollama.llm()
    if llm is not None:
        found["memory"] = found["chat"] = llm
    embed = _env_embed(env) or ollama.embed()
    if embed is not None:
        found["embedding"] = embed
    return found


def config_from_discovery(found: Mapping[str, Found]) -> Dict[str, Any]:
    """把找到的模型写成 config.json 的结构。key 只写环境变量名，不写 key 本身。"""
    models: Dict[str, Any] = {}
    roles: Dict[str, str] = {}
    for role, item in found.items():
        cfg = item.config
        name = cfg.model.split("/")[-1]
        spec: Dict[str, Any] = {"model": cfg.model, "base_url": cfg.base_url}
        if cfg.api != "openai":
            spec["api"] = cfg.api
        if cfg.api_key_env:
            spec["api_key_env"] = cfg.api_key_env
        if role == "embedding":
            spec["embed_batch_size"] = cfg.embed_batch_size
        models[name] = spec
        roles[role] = name
    semantic = "embedding" in found
    if not semantic:
        models["hash-embed"] = {"_说明": "本地哈希向量：不联网，只有词面相似度。有了 embedding 服务后换掉并运行 memsys reindex",
                                "model": "hash-%d" % HASH_DIM, "base_url": "local:hash",
                                "extra_body": {"dim": HASH_DIM}}
        roles["embedding"] = "hash-embed"
    return {"_说明": "memsys init 根据当前环境生成。没写的角色运行时会自动查找；key 只放在环境变量里。",
            "models": models, "roles": roles,
            "memory": {"query_expansion": not semantic}}
