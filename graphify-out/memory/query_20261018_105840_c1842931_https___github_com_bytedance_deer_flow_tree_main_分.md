---
type: "query"
date: "2026-10-18T10:58:40.443271+00:00"
question: "https://github.com/bytedance/deer-flow/tree/main 分析我的记忆系统和里面的有什么区别"
contributor: "graphify"
outcome: "useful"
source_nodes: ["MemorySystem", "Writer", "Retriever", "Store", "Fact"]
---

# Q: https://github.com/bytedance/deer-flow/tree/main 分析我的记忆系统和里面的有什么区别

## Answer

Expanded from original query via graph vocab: [memory, fact, retrieve, writer, store, source, temporal, budget]. The graph is dated 2026-10-10 and was used only for navigation; current local source was verified directly.

对比本地 memsys 当前代码与 DeerFlow main 固定快照 73f6749068b097038960c9aef528cf14bd4f4f09（本次读取）：memsys 偏向有来源支持的细粒度长期事实检索，默认 DeerMem 偏向用户画像、偏好与纠正信息的持续注入。DeerFlow 还有可替换外部后端，结论限于默认 DeerMem。

memsys 将原文、事实、来源、实体关系与任务统一保存在 Qdrant。原文先持久化，整批一次 LLM 抽取，完整字段精确去重；变化新增，保留旧内容，不再自动裁决取代。默认事实模式使用 BGE-M3 稠密向量、确定性词法稀疏检索、RRF 融合、最多两跳关系扩展和 BGE 重排。长期检索不自动用原文兜底，来源需明确读取。工作窗口保留最近12个完整对话轮，另有独立预算，退出窗口前须成功整理。事实区分事件时间、记录时间、事件状态和累计/增量语义。后台整理任务持久化，使用租约、心跳和失败状态恢复。

当前 DeerMem 的权威存储是用户全局 JSON 摘要与 agent 目录下每事实 Markdown；SQLite FTS5/BM25 是可重建检索索引。middleware 默认在每轮后入内存队列，30秒防抖、最多等待300秒后抽取；默认注入用户/历史摘要和置信度排序的事实，常规预算2000 tokens，correction 有保留预算。工具模式将事实放在 memory_search 后，搜索默认 FTS5，相关性与多样性词法排序可选；不能把该默认检索说成向量检索。短期会话滚动摘要是独立机制。支持事实强化、带门控的矛盾删除/替换、过期复查，可选有损合并默认关闭。队列是进程内存，优雅退出排空，丢失或失败更新依靠下一轮全对话补送，不等同于持久任务队列。

DeerMem 自动写入采用 user/thread/project、durability、authority 标签并确定性验收，只接收 user+durable+descriptive 的事实；不是把项目事实也分别持久化到项目库。memsys 当前有用户/namespace 隔离和语义限定条件，但缺少等价的一组跨任务范围及授权门控。DeerMem 有 correction/sourceError 与优先注入；memsys 没有同等独立的纠正保留机制。可借鉴范围标签和分层注入，同时保留 memsys 的事件历史与证据细节，不能照搬只允许长期用户画像的过滤策略。

主要本地证据：src/memsys/write.py:261,412；update.py:32；read.py:259,371,431；schema.py:91；working_memory.py:15,44；serve.py:483,505,580；core.py:188；config.py:29。上游证据：agents/memory/backends/deermem/deer_mem.py:215,309,356；deermem/core/storage.py:1；core/retrieval.py:612；core/queue.py:1；core/updater.py:140,2386；core/prompts/memory_update.chat.yaml；backend/docs/summarization.md。这是源码架构比较，没有进行双方统一模型、统一历史与统一预算的配对评测，不能据此声称准确率更高。

## Outcome

- Signal: useful

## Source Nodes

- MemorySystem
- Writer
- Retriever
- Store
- Fact