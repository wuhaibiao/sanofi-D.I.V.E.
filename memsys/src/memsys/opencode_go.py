"""OpenCode model catalog: user-selected free Zen models precede Go models."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

BASE_URL = "https://opencode.ai/zen/go/v1"
MODELS_URL = BASE_URL + "/models"
ZEN_BASE_URL = "https://opencode.ai/zen/v1"
# Canonical API IDs from the Zen catalog, rather than display-name spellings.
FREE_PRIORITY: Tuple[str, ...] = (
    "ling-3.0-flash-fin-free", "ling-3.1-flash-free", "longcat-2.5-preview-free",
    "mimo-v2.6-flash-free", "muse-spark-1.3-contributor-free", "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
)
# 不确定来源的实验型号放在最后；列表之外的新型号也放在最后。
PRIORITY: Tuple[str, ...] = FREE_PRIORITY + (
    "gpt-6-luna", "gpt-5.6-luna", "claude-haiku-5-5", "grok-4.7", "grok-4.6",
    "muse-spark-1.3-contributor", "muse-spark-1.2-contributor",
    "deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-flash",
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1",
    "kimi-k3", "kimi-k2.7-code", "kimi-k2.6",
    "qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus",
    "minimax-m3", "minimax-m2.7", "minimax-m2.5",
    "mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.5-pro", "mimo-v2.5",
    "hy4-preview", "hy3", "longcat-2.0", "step-5-preview-free",
    "deepseek-v4-flash-vision-exp", "space-bunny", "omen-alpha",
)
_EXISTING_NAMES = {
    "deepseek-v4.1-flash": "opencode-deepseek-flash",
    "deepseek-flash": "opencode-deepseek-flash-alias",
    "glm-5.3-flash": "opencode-glm-flash",
    "mimo-v2.6-flash": "opencode-mimo-flash",
    "kimi-k2.6": "opencode-kimi",
}


def prioritized_ids(available: Iterable[str]) -> List[str]:
    ranks = {model: index for index, model in enumerate(PRIORITY)}
    return sorted(set(available), key=lambda model: (ranks.get(model, len(ranks)), model))


def profile_name(model: str) -> str:
    if model in FREE_PRIORITY:
        return "opencode-zen-" + model
    return _EXISTING_NAMES.get(model, "opencode-" + model)


def model_spec(model: str) -> Dict[str, Any]:
    spec: Dict[str, Any] = {"model": model, "base_url": ZEN_BASE_URL if model in FREE_PRIORITY else BASE_URL,
                           "api_key_env": "OPENCODE_API_KEY",
                           "timeout_s": 180, "max_retries": 2, "pricing": {
                               "input": None, "output": None, "cached_input": None,
                               "currency": None, "as_of": None, "source": None}}
    if model in FREE_PRIORITY:
        spec['pricing'] = dict(input=0, output=0, cached_input=0, currency='USD',
                               as_of='2026-10-11', source='https://opencode.ai/docs/zen/#pricing')
    if model.startswith(("gpt-", "grok-", "muse-spark-")):
        spec["api"] = "responses"
        if model.startswith("gpt-"):
            spec["extra_body"] = {"reasoning": {"effort": "none"}}
        else:
            spec["output_token_headroom"] = 4096
            if model.startswith("muse-spark-"):
                spec["json_mode"] = False
    elif model.startswith(("claude-", "qwen", "minimax-")):
        spec["api"] = "anthropic"
        spec["json_mode"] = False
        spec["extra_body"] = {"thinking": {"type": "disabled"}}
    else:
        spec["api"] = "openai"
        if model in ("kimi-k3", "kimi-k2.7-code"):
            spec["extra_body"] = {"thinking": {"type": "enabled"}, "temperature": 1,
                                  "reasoning_effort": "low"}
            spec["output_token_headroom"] = 8192
            spec["json_mode"] = False
        elif model.startswith("glm-"):
            # 部分 GLM 上游拒绝 thinking 参数，保留其默认模式并另给推理余量。
            spec["output_token_headroom"] = 8192
        elif model.startswith(("deepseek-", "mimo-", "kimi-")):
            spec["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            spec["json_mode"] = False
            spec["output_token_headroom"] = 4096
    return spec


def configure_free(data: Dict[str, Any], available: Iterable[str]) -> Dict[str, Any]:
    """Use available user-selected free models; retain unrelated profiles and embeddings."""
    available = set(available)
    ordered = [model for model in FREE_PRIORITY if model in available]
    if not ordered:
        raise ValueError('none of the preferred free Zen models is available')
    models = data.setdefault('models', {})
    for model in FREE_PRIORITY:
        models[profile_name(model)] = model_spec(model)
    primary = profile_name(ordered[0])
    roles = data.setdefault('roles', {})
    for role in ('chat', 'query', 'judge'):
        roles[role] = primary
    roles['memory'] = profile_name('longcat-2.5-preview-free') if 'longcat-2.5-preview-free' in ordered else primary
    for role in ('extract', 'resolve'):
        if role in roles:
            roles[role] = roles['memory']
    if 'rerank' in roles:
        roles['rerank'] = primary
    fallbacks = data.setdefault('fallbacks', {})
    # A current free selection can only switch to another catalog-confirmed free model.
    for name in {primary, roles['memory']}:
        fallbacks[name] = [profile_name(model) for model in ordered if profile_name(model) != name]
    return data


def configure(data: Dict[str, Any], available: Iterable[str]) -> Dict[str, Any]:
    """更新给定配置；保留其他服务商、embedding 和记忆参数。"""
    ordered = prioritized_ids(available)
    if not ordered:
        raise ValueError("OpenCode Go returned no models")
    models = data.setdefault("models", {})
    for model in ordered:
        models[profile_name(model)] = model_spec(model)
    primary = profile_name(ordered[0])
    roles = data.setdefault("roles", {})
    for role in ("chat", "memory", "judge"):
        roles[role] = primary
    for role in ("extract", "resolve", "query", "rerank"):
        if role in roles:
            roles[role] = primary
    fallbacks = data.setdefault("fallbacks", {})
    # 原 OpenCode 候选链移到统一的海外优先主模型，避免留下互相矛盾的链。
    for name in list(fallbacks):
        if name in models and models[name].get("base_url", "").rstrip("/") in (BASE_URL, ZEN_BASE_URL):
            del fallbacks[name]
    fallbacks[primary] = [profile_name(model) for model in ordered[1:]]
    return data
