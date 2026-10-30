---
type: "query"
date: "2026-10-15T13:17:48.010511+00:00"
question: "同理分析/Users/OleniusBlanc227/Documents里面的sanofi文件夹，这个记忆系统的架构设计，评测集设计等，要求三点要点，评测集要有一个"
contributor: "graphify"
outcome: "useful"
source_nodes: ["MemorySystem", "Store", "Writer", "Retriever", "Runner"]
---

# Q: 同理分析/Users/OleniusBlanc227/Documents里面的sanofi文件夹，这个记忆系统的架构设计，评测集设计等，要求三点要点，评测集要有一个

## Answer

Expanded from original query via vocab: [memory, relational, working, structure, retrieval, budget, eval, longmemeval, integrity, coverage, source].
用户要将 sanofi 记忆系统整理为三个工作/项目经历要点，评测集必须单独占一个。已核对当前 README、architecture.md、store.py、read.py、working_memory.py、question_serial_eval.py、evaluation-integrity.md 和既有聚合报告；未修改生产代码、未运行模型评测、未查看评测题目或标准答案。
1. 长期记忆服务架构：Python 与宿主模型解耦；Qdrant 是唯一在线主存储，统一保存会话、事实、来源、实体关系和任务；HTTP/MCP/Web 接入。消息幂等、持久任务、不可变版本、prepared/committed操作日志和租户发布头条件更新支持后台整理、冲突重试及中断恢复。Qdrant应用提交协议不等于原生SQL多记录事务。依据 docs/architecture.md:3,40,81,96，src/memsys/store.py:167。
2. 跨会话检索及上下文：事实 Dense+Sparse 混合召回、融合、BGE重排、最多2跳实体关系扩展、事实ID去重、时间和生命周期过滤、预算与覆盖声明；完整轮工作窗口淘汰前必须成功整理。默认 facts 不把历史来源正文补入长期记忆上下文。依据 docs/architecture.md:42,46,50,114，src/memsys/read.py:269,371，working_memory.py:15,40。
3. 评测设计：LongMemEval六类能力，V7 32题含跨会话8题、时间8题、其他四类各4题；write/read/judge分阶段字段白名单与物理隔离，源码/配置/输入哈希冻结，记录分类型正确率、usage、耗时与trace。后续新增question_serial_eval逐题独立建库、写入、审计、单次回答、清理并验证空库，最后独立评分进程；该新编排不能套用于旧32题批量按corpus隔离的结果。依据 docs/evaluation-integrity.md:9,17,19 和 scripts/question_serial_eval.py:38,114,201。
历史冻结32题报告28/32(87.5%)，原文摘录0；同模型判分、Oracle限定历史、小样本。后续system-handbook审计的historical_exposure.confirmed_existing_answer_overlap=30，不能把这32题称为未使用盲测或据此证明当前总体准确率。依据 runs/relational-memory-v7-32-20261014/report.md:3,31,55，runs/system-handbook-audit-20261014/audit.json。README当前文档中关于fresh cohort的旧表述应以此更正为准。

## Outcome

- Signal: useful

## Source Nodes

- MemorySystem
- Store
- Writer
- Retriever
- Runner