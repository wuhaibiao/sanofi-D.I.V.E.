# 历史评测报告

这21轮是已有 LongMemEval Oracle 开发评测，保留原始判分和流程失败。相同组号表示原题与顺序相同，不同组不直接作版本优劣对比；定向复测不能换算为整组准确率。

| 版本 | 题组 | 原始判分 | 正常交付 | 报告 |
|---|---|---:|---:|---|
| 10月25日 列式抽取初版 | A40 | 15/40 | 16/40 | [原始报告](memory-regression40-20261025/report.md) |
| 10月25日 对象格式修复 | A40 | 32/40 | 40/40 | [原始报告](memory-regression40-objects-20261025/report.md) |
| 10月25日 语义边界修复 | A40 | 37/40 | 40/40 | [原始报告](memory-regression40-semantic-20261025/report.md) |
| 10月25日 30题对照版本 | B30 | 25/30 | 30/30 | [原始报告](memory-control30-semantic-20261025/report.md) |
| 10月26日 硬校验版本 | B30 | 21/30 | 23/30 | [原始报告](memory-regression30-contracts-20261026/report.md) |
| 10月26日 七道流程失败题复测 | 定向7 | 3/7 | 7/7 | [原始报告](memory-regression7-system-flow-20261026/report.md) |
| 10月26日 移除硬规则 三题复测 | 定向3 | 3/3 | 3/3 | [原始报告](memory-regression3-audit-only-20261026/report.md) |
| 10月26日 校验仅作诊断 | C20 | 18/20 | 20/20 | [原始报告](memory-regression20-audit-only-20261026/report.md) |
| 10月26日 记忆保真 两题复测 | 定向2 | 2/2 | 2/2 | [原始报告](memory-regression2-fidelity-20261026/report.md) |
| 10月26日 记忆保真 完整复测 | C20 | 18/20 | 19/20 | [原始报告](memory-regression20-fidelity-20261026/report.md) |
| 10月27日 抽取容错 两题复测 | 定向2 | 2/2 | 2/2 | [原始报告](memory-regression2-task-context-20261027/report.md) |
| 10月27日 移除回答审计 | C20 | 20/20 | 20/20 | [原始报告](memory-regression20-no-answer-audit-20261027/report.md) |
| 10月27日 主干 另一组20题 | D20 | 19/20 | 20/20 | [原始报告](memory-additional20-no-answer-audit-20261027/report.md) |
| 10月28日 模型事实筛选分支 | D20 | 18/20 | 20/20 | [原始报告](evidence-selection-regression20-20261028/report.md) |
| 10月28日 紧凑写入 Top 10 | D20 | 18/20 | 20/20 | [原始报告](compact-ranked-regression20-20261028/report.md) |
| 10月28日 来源优先 Top 20 | D20 | 18/20 | 20/20 | [原始报告](top20-refinement-regression20-20261028/report.md) |
| 10月28日 Top 20 返回与表格改进 | D20 | 17/20 | 20/20 | [原始报告](top20-answer-context-regression20-20261028/report.md) |
| 10月29日 返回层压缩分支 | D20 | 16/20 | 19/20 | [原始报告](return-layer-regression20-20261029/report.md) |
| 10月29日 主干 另一组30题 | E30 | 24/30 | 30/30 | [原始报告](main-additional30-20261029/report.md) |
| 10月29日 原文分块初版 | D20 | 15/20 | 20/20 | [原始报告](raw-chunk-regression20-20261029/report.md) |
| 10月29日 原文分块 选择与返回改进 | D20 | 18/20 | 20/20 | [原始报告](raw-chunk-context20-20261029/report.md) |

汇总用量和耗时见[评测目录](../proposal-draft-20261029/evaluation-catalog.json)。原始请求、完整响应及临时存储核验记录继续保存在本地运行目录，未作为部署材料打包。
