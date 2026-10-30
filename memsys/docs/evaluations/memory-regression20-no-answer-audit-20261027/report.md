# 移除回答审计后：固定20题完整复测

结果：**20/20判分正确，20/20正常交付**；上一轮同20题为18/20、19/20。

内容复核仍发现未解决项：通用化仍存在：用户的腰果基底酸奶目标已保存并召回，相关浸泡、搅拌、发酵和过滤建议也在所选记忆中。实际正文仅用牛肉炖菜经历开头，随后给出水量、分层、开盖、装填量、时间和普通植物性餐食的建议，未提及酸奶或腰果，也未给出对应目标的操作建议。当前断点仍在回答没有应用已到达提示的具体目标。评分模型本次仍判true；保留原始标签，不将20/20解释为个性化问题已经解决。

逐题：改善2题，回退0题，持续正确18题，两轮均未正确0题。答题流程错误0，评分错误0。

| 题型 | 上轮正确 | 本轮正确 | 改善 | 回退 |
|---|---:|---:|---:|---:|
| single-session-user | 3/3 | 3/3 | 0 | 0 |
| single-session-assistant | 3/3 | 3/3 | 0 | 0 |
| single-session-preference | 3/3 | 3/3 | 0 | 0 |
| knowledge-update | 3/3 | 3/3 | 0 | 0 |
| multi-session | 2/4 | 4/4 | 2 | 0 |
| temporal-reasoning | 4/4 | 4/4 | 0 | 0 |

拒答题：上轮4/4，本轮4/4。

| 题号 | 题型 | 上轮 | 本轮 | 变化 |
|---|---|---|---|---|
| lme_bc8a6e93_abs | single-session-user | 正确 | 正确 | stable_correct |
| lme_25e5aa4f | single-session-user | 正确 | 正确 | stable_correct |
| lme_caf9ead2 | single-session-user | 正确 | 正确 | stable_correct |
| lme_1d4da289 | single-session-assistant | 正确 | 正确 | stable_correct |
| lme_6ae235be | single-session-assistant | 正确 | 正确 | stable_correct |
| lme_4388e9dd | single-session-assistant | 正确 | 正确 | stable_correct |
| lme_505af2f5 | single-session-preference | 正确 | 正确 | stable_correct |
| lme_caf03d32 | single-session-preference | 正确 | 正确 | stable_correct |
| lme_1da05512 | single-session-preference | 正确 | 正确 | stable_correct |
| lme_0ddfec37_abs | knowledge-update | 正确 | 正确 | stable_correct |
| lme_852ce960 | knowledge-update | 正确 | 正确 | stable_correct |
| lme_1cea1afa | knowledge-update | 正确 | 正确 | stable_correct |
| lme_e5ba910e_abs | multi-session | 正确 | 正确 | stable_correct |
| lme_eeda8a6d | multi-session | 错误 | 正确 | improved |
| lme_gpt4_a56e767c | multi-session | 错误 | 正确 | improved |
| lme_d6062bb9 | multi-session | 正确 | 正确 | stable_correct |
| lme_gpt4_70e84552_abs | temporal-reasoning | 正确 | 正确 | stable_correct |
| lme_d01c6aa8 | temporal-reasoning | 正确 | 正确 | stable_correct |
| lme_2ebe6c90 | temporal-reasoning | 正确 | 正确 | stable_correct |
| lme_gpt4_0a05b494 | temporal-reasoning | 正确 | 正确 | stable_correct |

## 本次执行与完整性

32段历史、364条原始消息；逐字保留True，完成记忆处理364条。失败来源会话0，记录到隔离或失败的来源会话0。原文摘录0、待处理原文0。

实际调用{'extract': 32, 'embed': 202, 'judge': 20, 'embed_query': 20, 'answer': 20}；配置内传输重试0次。提供方返回的输出触顶／未完成记录0条，详情保留在evaluation-summary.json，未按最终得分重答。

回答审计记录0，可见审计信封0；20条正常交付正文均与提供方最终响应一致。全部答题结束后才开始评分：True。

源码、运行配置、原题及评分规则在运行期间保持冻结；旧记录文件862个和既有集合payload前后核验一致。临时集合全部清理。总耗时52.4分钟。

流程核对全部通过：True。逐项结果见evaluation-summary.json；流程成功不证明记忆内容语义完整。

## 内容观察与解释边界

- lme_4388e9dd：衣着细节本次保存、召回并用于正文，回答明确给出不整洁、有污渍的白衬衫。事实仍属于助手所写剧本的角色属性；本次没有复现旧的衣着漏存问题，但一次成功不证明抽取持续稳定。
- lme_caf03d32：通用化仍存在：用户的腰果基底酸奶目标已保存并召回，相关浸泡、搅拌、发酵和过滤建议也在所选记忆中。实际正文仅用牛肉炖菜经历开头，随后给出水量、分层、开盖、装填量、时间和普通植物性餐食的建议，未提及酸奶或腰果，也未给出对应目标的操作建议。当前断点仍在回答没有应用已到达提示的具体目标。评分模型本次仍判true；保留原始标签，不将20/20解释为个性化问题已经解决。
- lme_gpt4_a56e767c：电影节计数本次回答4个，并包含上轮漏计的Austin Film Festival。参加拍片挑战的事实已保存、召回并进入实际提示。本次未复现漏计；与此前两题复测的单次通过相符，但不证明跨次生成始终稳定。

原始评分完整保留；内容观察不改写标签。本轮是已经暴露的同题开发回归。基线版本早于此前的抽取隔离和任务组织修复，因此是版本整体对照，不能把分数变化单独归因于去掉审计。评分与回答沿用同一配置模型家族，单独评分阶段不等于第三方独立评测。

## 原始记录

- [全部结果与流程指标](evaluation-summary.json)；[逐题对照](paired-comparison.json)
- [全部真实回答](LongMemEval-answers.jsonl)；[原始评分](LongMemEval-grades.jsonl)
- [提供方正文一致性](provider-response-integrity.json)；[原始回答响应](LongMemEval-read-responses.jsonl)
- [冻结和执行方法](freeze.json)；[预先冻结的验收口径](assessment-plan.json)；[阶段输入一致性](phase-proof.json)
- [执行顺序与清理](execution.json)；[既有数据及源码核对](final-integrity.json)
