---
type: "query"
date: "2026-10-14T03:55:35.160918+00:00"
question: "每个评测集题型选择4道题，评测一下；我的目录下不是有LongMemEval吗，为什么还选择其他的评测集"
contributor: "graphify"
outcome: "corrected"
correction: "评测范围应按用户指定的本地 LongMemEval 收敛，不应把历史研究材料中的其他数据集自动纳入。最终六类各4题，共24题。"
source_nodes: ["Runner", "load()", "judge()", "MemorySystem"]
---

# Q: 每个评测集题型选择4道题，评测一下；我的目录下不是有LongMemEval吗，为什么还选择其他的评测集

## Answer

Expanded query tokens from graph vocabulary: runner judge load replay memory。最初错误地把历史研究目录的 ConvoMem 和 LoCoMo 也纳入评测；用户澄清后所有额外评测进程已停止，最终仅使用本地 memsys/data/longmemeval_oracle.json。保留先前固定的六种原始 question_type 各四道可回答题，共24题，没有根据实际结果重新选题。同一当前源码与本机配置的写入继续复用已完成会话，所有24个独立语料账户、40段会话、477条原始消息校验一致并全部整理。通过当前 WebChat 同款检索及回答提示词生成24个答案，没有临时待整理原文使用。GPT-6 Luna 原始 Boolean 语义评分24/24，六类题型各4/4；抽取、回答、评分失败均0。单题检索与回答平均10.876秒，中位10.393秒。报告位于 memsys/runs/longmemeval-types-four-20261013/report.md，实际答案与原始评分完整保留。仅为已使用 Oracle 题库的小规模抽样回归，非全量准确率、独立盲测或官方榜单成绩，回答与评分均使用GPT-6 Luna。

## Outcome

- Signal: corrected
- Correction: 评测范围应按用户指定的本地 LongMemEval 收敛，不应把历史研究材料中的其他数据集自动纳入。最终六类各4题，共24题。

## Source Nodes

- Runner
- load()
- judge()
- MemorySystem