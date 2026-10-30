"""评测来源记录：源码、提示词和模型参数；不保存凭据。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import ast
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

from .. import prompts


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def model_details(model: Any, name: str = "") -> Dict[str, Any]:
    if hasattr(model, "candidate_names"):
        return {"adapter": type(model).__name__, "policy": "ordered_quota_or_unavailable",
                "cooldown_s": model.cooldown_s,
                "candidates": [model_details(client, candidate)
                               for client, candidate in zip(model.clients, model.candidate_names)]}
    config = getattr(model, "config", None)
    if config is None:
        return {"name": name, "adapter": type(model).__name__}
    url = urlsplit(config.base_url)
    host = url.netloc.rsplit("@", 1)[-1]
    options = {k: v for k, v in config.extra_body.items()
               if k in ("seed", "top_p", "top_k", "temperature", "frequency_penalty", "presence_penalty",
                        "reasoning_effort", "max_tokens")}
    return {"model": config.model, "base_url": urlunsplit((url.scheme, host, url.path, "", "")),
            "api": config.api, "timeout_s": config.timeout_s, "max_retries": config.max_retries,
            "output_token_headroom": config.output_token_headroom,
            "json_mode": config.json_mode, "embed_batch_size": config.embed_batch_size,
            "embed_max_input_bytes": config.embed_max_input_bytes,
            "embedding_pooling": "utf8_weighted_unit_mean_v1" if config.embed_max_input_bytes else "single_input",
            "generation_options": options, "extra_body_sha256": fingerprint(config.extra_body),
            "pricing": asdict(config.pricing)}


def model_manifest(models) -> Dict[str, Any]:
    roles = {"chat": models.chat, "memory": models.memory, "embedding": models.embedder,
             **{p: models.for_purpose(p) for p in ("extract", "resolve", "query", "rerank")}}
    return {role: model_details(model, models.names.get(role, models.names.get("memory", "")))
            for role, model in roles.items() if model is not None}


def prompt_fingerprint() -> str:
    from ..webchat import SYSTEM
    from ..task_context import ADVICE_CONTEXT
    from ..advice_intent import CAUSE_ANALYSIS_RULES
    from ..quantity_scope import QUANTITY_RELATION_RULES
    from ..aggregate_batch import RULES
    from ..evidence_batch import RULES as EVIDENCE_RULES
    from ..aggregate_batch import SEMANTIC_PAGE_RULES
    from ..semantic_review import RECONCILE_RULES, SELECTION_RULES, LOCAL_SELECTION_RULES, RESPONSE_RULES, DATE_RESPONSE_RULES
    values = {k: v for k, v in vars(prompts).items() if k.isupper() and isinstance(v, str)}
    values['WEBCHAT_SYSTEM'] = SYSTEM
    values['ADVICE_CONTEXT'] = ADVICE_CONTEXT
    values["CAUSE_ANALYSIS_RULES"] = CAUSE_ANALYSIS_RULES
    values["QUANTITY_RELATION_RULES"] = QUANTITY_RELATION_RULES
    values["AGGREGATE_PAGE_RULES"] = RULES
    values["EVIDENCE_PAGE_RULES"] = EVIDENCE_RULES
    values.update(SEMANTIC_PAGE_RULES=SEMANTIC_PAGE_RULES, RECONCILE_RULES=RECONCILE_RULES,
                  SELECTION_RULES=SELECTION_RULES, LOCAL_SELECTION_RULES=LOCAL_SELECTION_RULES,
                  RESPONSE_RULES=RESPONSE_RULES, DATE_RESPONSE_RULES=DATE_RESPONSE_RULES)
    return fingerprint(values)


def source_manifest() -> Dict[str, Any]:
    package = Path(__file__).resolve().parents[1]
    hashes = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(package.rglob("*.py"))}
    write_files = ("update.py", "fact_content.py", "structure_memory.py", "memory_compaction.py", "relational_memory.py", "budget.py", "lifecycle.py", "textutil.py",
                   "timeutil.py", "vecmath.py", "llm.py", "anthropic_client.py", "protocols.py", "fallback.py",
                   "generation.py", "write_coverage.py", "extraction_protocol.py")
    # 检索分块、索引迁移与重排不使同一事实库失效。抽取/裁决的函数仍逐个冻结。
    fact_sources = {k: hashes[k] for k in write_files if k in hashes}
    select = {"write.py": {"Round", "ExtractPart", "NewFact", "salience", "group_turns", "build_rounds", "_dedupe",
                          "_parts", "_extract", "_decode_extraction", "_extraction_diagnostic", "_extract_chunk", "_record_spans", "_normalize_facts",
                          "_literal_facts", "restore_structures"},
              "store.py": {"add_fact", "fact", "facts", "sources", "facts_of_episodes", "record_span",
                           "add_sources", "reinforce", "supersede", "set_status", "transaction", "_fact"},
              "schema.py": {"Fact", "normalize_semantics"}}
    for filename, names in select.items():
        tree = ast.parse((package / filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
                fact_sources[filename + ":" + node.name] = ast.dump(node, include_attributes=False)
    result = {"source_sha256": fingerprint(hashes),
              "write_source_sha256": fingerprint(fact_sources),
              "index_source_sha256": fingerprint({name: hashes[name] for name in ("indexing.py", "fact_content.py")})}
    root = package.parents[1]
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=2)
        if commit.returncode == 0:
            result["git_commit"] = commit.stdout.strip()
            status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "src"],
                                    capture_output=True, text=True, timeout=2)
            result["uncommitted_source"] = bool(status.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def answer_model_settings(manifest: Dict[str, Any]) -> Dict[str, Any]:
    # 价格只影响账本，不影响回答或已有的写入缓存。
    def without_pricing(value):
        if isinstance(value, dict):
            return {key: without_pricing(item) for key, item in value.items() if key != "pricing"}
        if isinstance(value, list):
            return [without_pricing(item) for item in value]
        return value
    return without_pricing(manifest)
