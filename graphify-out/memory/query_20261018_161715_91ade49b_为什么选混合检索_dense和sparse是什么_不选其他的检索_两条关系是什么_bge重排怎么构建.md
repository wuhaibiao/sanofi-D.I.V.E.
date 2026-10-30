---
type: "query"
date: "2026-10-18T16:17:15.995398+00:00"
question: "为什么选混合检索，Dense和Sparse是什么，不选其他的检索；两条关系是什么；BGE重排怎么构建、取top几；上下文预算多大、放不下怎么办；明确标记覆盖不足是什么"
contributor: "graphify"
outcome: "useful"
source_nodes: ["Retriever", ".rerank()", "reranking.py", "budget_counter()", "Selection", "MemorySystem"]
---

# Q: 为什么选混合检索，Dense和Sparse是什么，不选其他的检索；两条关系是什么；BGE重排怎么构建、取top几；上下文预算多大、放不下怎么办；明确标记覆盖不足是什么

## Answer

Expanded via exact graph vocabulary: retrieval rerank budget coverage context fact selection search. The existing graph is stale and was used for navigation; current memsys/src/memsys source and allowlisted local configuration were verified. Dense uses BGE-M3 semantic embeddings; sparse is deterministic hashed lexical matching with Qdrant IDF, not standard BM25 or BGE neural sparse. terms() deduplicates terms before Counter, so do not claim full term-frequency weighting or BM25 saturation/length normalization. Independent retrieval streams each return top30, then merge by fact ID with RRF-like fusion k60; Dense contribution also multiplies nonnegative similarity, so it is not pure standard RRF. Entity relation expansion is a separate bounded graph traversal of stored fact-backed edges: at most2 hops,6 seed facts when seeding through facts,32 nodes96 edges32 relation candidates. Query-matched entity names can also seed expansion; status, validity, ended/cancelled closures and ambiguous aliases are checked. Related candidate facts enter the same reranking pool. BGE uses pretrained BAAI/bge-reranker-v2-m3 sequence classification on question plus full fact text and metadata. At most60 candidates are scored; pair windows max512 real tokenizer tokens, overlap32, per-fact max logit across windows, fp32 MPS batch4. These are relevance logits, not calibrated correctness probabilities. Unscored candidates remain at the fused tail. Current strict=true raises reranker failures; fusion fallback is optional when strict=false. Ordinary selection max30 facts; needs_all initial max60, optional host-provided larger coverage budget allows up to128. Exact selected count also depends on available rendered-text budget; not a fixed top5. Both local configuration files set budget_mode=utf8, context_budget_tokens32000 bytes, chat_window_tokens12000 bytes and12 whole completed rounds, answer_input_budget_tokens65536 bytes. These are application text budgets, not a65536-token native LLM window. Host subtracts system prompt, query, recent history, framing and safety reserve; initial memory budget is at most32000 and actual remaining input budget. Bare recall can further cap by history size or receive a caller budget. Facts are selected whole with metadata, low-priority candidates omitted and traced, then rendered chronologically and rechecked. Working-window eviction requires prior successful extraction. needs_all enumerates the eligible searchable fact pool including archives within aggregate_candidate_limit5000 rather than assuming ANN TopK is exhaustive. If the eligible pool exceeds this cap, exhaustive search is refused with search_complete=false. coverage.context_complete requires complete candidate enumeration plus no omitted facts/episodes; current warning is generated for recognized needs_all queries only, not every ordinary lookup. The warning says only a known subset is available and forbids claims of an exhaustive list or final total; host compliance is prompt-based, not a mathematical guarantee. Optional coverage supplement reuses the already retrieved facts and no new LLM extraction or raw archive reads. recall_pages is a bounded external pagination interface and not automatically consumed by the current single-answer adapter. Coverage completeness is relative to stored candidate scope, not semantic ground truth, extraction completeness or answer correctness. Primary official Qdrant hybrid/sparse documentation and the BAAI Hugging Face reranker model card were checked for general definitions. No code modifications, model runs, new evaluations or empirical superiority claims were made.

## Outcome

- Signal: useful

## Source Nodes

- Retriever
- .rerank()
- reranking.py
- budget_counter()
- Selection
- MemorySystem