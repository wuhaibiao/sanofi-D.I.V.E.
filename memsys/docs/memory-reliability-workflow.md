# 记忆系统可靠性工作流程

本轮工作只覆盖手册要求的写入、组织、更新、检索、复用、遗忘及其验证。先修复失败恢复与信息遗忘的闭环，再检查容量和跨会话语义，最后用当前源码重新评测。功能覆盖与真实效果分别验收，机械检查不能代替真实模型结果。

按用户最新要求，实际效果验收采用分题型代表题，不继续500题全量模型回放。最新同版本复测的固定34题为25/34（73.5%），基线18/34（52.9%）；未参与调试的14题留出组为12/14（85.7%），仅覆盖仍有未用题目的三个题型。扩展322项回归全部通过，源码、提示词与配置在两组评测及网络续跑前后保持一致。固定多会话题仍为1/6，且两道原正确题发生回退，因此仍有明确的语义缺口。完整修复、干扰因素与错题见 [最新修复与同版本评测报告](../runs/semantic-repair-v16/assessment.md)。此前 [v15定向记录](../runs/representative-fixes-v15/assessment.md) 及下文历史执行记录保留各自来源，不能作为当前准确率。

## 工作顺序与验收条件

| 顺序 | 工作 | 验收条件 | 实现或入口 |
|---|---|---|---|
| 1 | 不完整抽取的持久恢复 | 关闭会话、进程重启后仍可补抽；重复失败不出队；整轮完整后才提交事实；重试不重复强化 | `write.py`、`extract_backlog`、`recover`、`rebuild` |
| 2 | 信息遗忘 | 共享原文、向量、关键词、近期对话、补抽和索引重建都不能带回遗忘信息；其他有效原子事实保留 | `withheld`、`conversation_exclusions`、遗忘回归 |
| 3 | 生命周期与历史召回 | 活跃容量有默认上限；重要事实保留；历史、聚合及时间查询覆盖归档；维护重复执行不改变结果 | `check_lifecycle_pressure.py` |
| 4 | 工程与输入覆盖 | 全部测试通过；数据泄漏检查通过；选定样本输入覆盖无丢失和预算超限；记录检索规模变化 | 验收脚本的本地阶段 |
| 5 | 真实语义召回 | 自拟跨语言事实及用户、助手长原文尾部召回通过；与前缀索引比较；保留来源指纹 | `check_semantic_retrieval.py` |
| 6 | 真实回答行为 | 10 个固定记忆案例检查事件、累计值、数量、推荐、时间、历史版本；调用失败与答错分开 | `bench behavior` |
| 7 | 真实记忆机制 | 12 个原始会话案例实际经过抽取、更新、检索、回答，覆盖计划、实体别名和共享原文遗忘 | `bench mechanisms` |
| 8 | 代表题真实实验 | 固定各题型样本并记录指纹，完成每道样本答案及本地语义评分，披露覆盖、分题型结果、用量、费用状态、耗时与失败模式；后续修复单独定向复测 | `select_representative.py`、`bench run`、`bench judge`、`bench report` |

第 6、7 阶段使用自拟数据作为稳定回归门槛，参考结果只在模型完成回答后使用。精确输出检查可能把等价但不符合指令的表达判为失败，不能拿来替代 Oracle 的语义评分。

## 执行入口

在仓库根目录运行：

```bash
# 本地机制、输入覆盖、容量压力与检索规模
PYTHONPATH=src python3 scripts/validate_memory_system.py --config config.json --out runs/reliability-local

# 加入真实 embedding、回答行为与记忆机制回归
PYTHONPATH=src python3 scripts/validate_memory_system.py --config config.json --out runs/reliability-models --with-models

# 固定代表题，再重新建库、答题和本地评分
python3 scripts/select_representative.py --data data/longmemeval_oracle.json --out runs/representative-new
memsys bench run --config config.json --data runs/representative-new/sample.json --out runs/representative-new/oracle
memsys bench judge --config config.json --data runs/representative-new/sample.json --hyp runs/representative-new/oracle/hypotheses.jsonl
memsys bench report --data runs/representative-new/sample.json --run runs/representative-new/oracle
```

每个目录保存 `workflow.json`、`report.md` 和阶段日志。来源记录包含源码及提示词指纹；源码改变后使用新输出目录，避免把不同版本的结果混为一份结论。已通过的源码版本可以续跑同一目录，Oracle 自身还会核对模型、参数和数据一致性。

模型检查失败会记录阻塞并跳过依赖阶段，本地检查继续独立完成。回答和抽取模型需要恢复账户或配置可用服务；只有 embedding 的模型不能承担这两个角色。价格未配置时保留“未知费用”，不能把它填成免费。

## 当前修复与验证边界

持久补抽、共享原文屏蔽、近期对话排除和默认活跃容量已实现。历史与聚合查询会搜索归档，查询向量共用一次调用。验收脚本记录当前源码的实际执行结果，不能沿用旧版本的小样本或近似分数作为新版效果。

遗忘保守屏蔽整个来源轮次；同轮其他原子事实可继续使用，但未结构化原文细节不再提供。审计日志和归档仍保留，物理存储没有硬上限。新的用户发言可以重新建立记忆，此功能不会永久禁止用户再次陈述同一信息。

属性槽对齐、事件身份、别名、累计值与独立事件、支付与活动日期仍需真实语义验证。现有程序核验能检查引用、单位和算术，不能证明相关事件全部召回或事件身份判断正确。容量与候选预算是效率策略，完整输入也不能保证抽取模型完整理解了所有事实。

本地评分属于实验结果；正式统一评估由项目组按公布的评判模型复跑，单独记录，不并入本地验收通过状态。

## 本轮执行记录

最终代码版本的来源指纹为 `b4da1f0b1ebb91dd33b377081a3230470abb38a335791e98cc852c8361dba3e2`。完整阶段记录见 [验收报告](../runs/reliability-v4/report.md) 和 [来源及执行清单](../runs/reliability-v4/workflow.json)。

| 检查 | 实际结果 |
|---|---|
| 自动测试 | 221 项通过，包括抽取失败重启恢复、混合无效输出、共享原文遗忘、近期对话屏蔽及归档查询 |
| 评测数据泄漏检查 | 通过 |
| 500 题、5,483 轮输入与索引覆盖 | 无输入丢失、无预算超限；脚本使用空输出抽取器，未验证事实理解质量 |
| 百、千、万规模生命周期压力 | 各规模 6 项条件通过，包含容量、重要事实保留、历史召回和维护幂等性 |
| 真实 bge-m3 embedding | 自拟跨语言短事实 2/2、用户和助手长原文尾部 2/2 召回通过 |
| 本地检索规模 | 万条合成记忆检索 P95 556.2 ms；不含查询分析、重排或回答模型调用 |
| 真实抽取与回答 | DeepSeek 返回 HTTP 402 / Insufficient Balance，依赖阶段未执行 |

因此本轮已完成工程修复和本地验收，但整体流程状态仍是 `blocked`。待回答、抽取服务恢复后，继续第 6—8 阶段；未完成这些阶段前，不把旧版分数或输入覆盖率当成新版准确率。

## 后续系统工程补齐

六项非模型缺口已进入 [系统缺口工作流程](system-gap-workflow.md)：统一预算、聚合分页覆盖、旧失败补偿、来源可核验的细粒度遗忘、物理清理与容量上限、可选精确打分加速及600周期综合验收。下述既有结果属于先前源码版本，当前结论使用新流程中记录的目录与来源指纹。
