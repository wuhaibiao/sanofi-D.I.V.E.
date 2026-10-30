# 当前部署版本：固定20题完整复测

结果：18/20判分正确，19/20正常交付；上一轮同20题为18/20。
逐题变化：改善2题、回退2题、持续正确16题、两轮均未正确0题。流程回答错误1，评分错误0。

未通过本轮架构流程验收：仍有历史未完整处理并导致答案无法交付。

交付对照：上轮20/20，本轮19/20；本轮正常交付但判错1题。评分记录状态：{'graded': 19, 'answer_error': 1}。answer_error表示上游流程失败按错计分，没有对不存在的答案调用评分模型。

## 分类与逐题对照

| 题型 | 上轮 | 本轮 | 改善 | 回退 |
|---|---:|---:|---:|---:|
| single-session-user | 3/3 | 3/3 | 0 | 0 |
| single-session-assistant | 2/3 | 3/3 | 1 | 0 |
| single-session-preference | 2/3 | 3/3 | 1 | 0 |
| knowledge-update | 3/3 | 3/3 | 0 | 0 |
| multi-session | 4/4 | 2/4 | 0 | 2 |
| temporal-reasoning | 4/4 | 4/4 | 0 | 0 |

拒答题：上轮4/4，本轮4/4。

| 题号 | 题型 | 上轮 | 本轮 | 变化 |
|---|---|---|---|---|
| lme_bc8a6e93_abs | single-session-user | 正确 | 正确 | stable_correct |
| lme_25e5aa4f | single-session-user | 正确 | 正确 | stable_correct |
| lme_caf9ead2 | single-session-user | 正确 | 正确 | stable_correct |
| lme_1d4da289 | single-session-assistant | 正确 | 正确 | stable_correct |
| lme_6ae235be | single-session-assistant | 正确 | 正确 | stable_correct |
| lme_4388e9dd | single-session-assistant | 错误 | 正确 | improved |
| lme_505af2f5 | single-session-preference | 正确 | 正确 | stable_correct |
| lme_caf03d32 | single-session-preference | 错误 | 正确 | improved |
| lme_1da05512 | single-session-preference | 正确 | 正确 | stable_correct |
| lme_0ddfec37_abs | knowledge-update | 正确 | 正确 | stable_correct |
| lme_852ce960 | knowledge-update | 正确 | 正确 | stable_correct |
| lme_1cea1afa | knowledge-update | 正确 | 正确 | stable_correct |
| lme_e5ba910e_abs | multi-session | 正确 | 正确 | stable_correct |
| lme_eeda8a6d | multi-session | 正确 | 错误 | regressed |
| lme_gpt4_a56e767c | multi-session | 正确 | 错误 | regressed |
| lme_d6062bb9 | multi-session | 正确 | 正确 | stable_correct |
| lme_gpt4_70e84552_abs | temporal-reasoning | 正确 | 正确 | stable_correct |
| lme_d01c6aa8 | temporal-reasoning | 正确 | 正确 | stable_correct |
| lme_2ebe6c90 | temporal-reasoning | 正确 | 正确 | stable_correct |
| lme_gpt4_0a05b494 | temporal-reasoning | 正确 | 正确 | stable_correct |

此前定向复测的2道错题，在本次完整重跑中2/2判分正确。

## 流程失败诊断

- lme_eeda8a6d：JSON解析及facts数组检查之后，整批结构检查抛错，导致该段历史未发布；随后因来源入库不完整而未答题。
  - 失败会话处理0/12条消息；该调用用量[{'completion_tokens': 3784, 'retries': 0, 'latency_s': 29.594198416918516}]。
  - 原始失败抽取正文未保存，聚合错误未指出具体条目，不能确定究竟是哪一个字段或轮次无效。调用用量和原始错误保留，未补抽或重答。

## 内容观察

- lme_4388e9dd：衣着题本次完整保留并召回了角色衣着属性，答案明确给出不整洁、有污渍的白衬衫；原先的写入漏存本次未复现。该事实带有剧本角色语境，没有将角色属性当作用户本人的属性。单次成功仍不能证明抽取始终稳定。
- lme_caf03d32：慢炖锅题本次仍然通用化。67条已存事实中选中30条，前7条均为用户相关事实；第6条明确保留腰果基底酸奶目标，另有酸奶配方、调味及过滤请求，助手制作步骤也被召回。但实际答案只提及牛肉炖菜成功经历，随后给出水量、开盖、食材位置等通用慢炖建议，没有提到酸奶、腰果目标或相应操作。故本次主要断点是回答阶段未应用已经到达上下文的目标，不能再以漏存或未召回来解释。此前两题定向复测的通过未能证明这一行为稳定。
- lme_gpt4_a56e767c：电影节计数题本次漏计Austin Film Festival：用户在原始历史中多次说明参加该电影节的48小时拍片挑战；相应事实已保存、召回，并完整出现在实际答题提示中。80条事实全部进入本次选择，答案仍仅列Portland、Seattle和AFI三项并回答3。原题参考为4，历史本身也支持四项；主断点是回答阶段没有完整汇总已到达上下文的事件，不能归因于漏存、未召回或输出截断。

内容观察独立于原始模型评分：其中目标已召回但未用于建议的样本，本轮仍被模型判为正确，说明该评分未能反映这一缺陷。原始true标签完整保留，没有人工改分。

## 架构执行核验

32段历史、364条原始消息导入；逐字保留：True；完成记忆处理352条，未完整处理会话：1。召回原文摘录0、待处理原文0。

全部答题流程结束后才评分。实际调用：{'extract': 32, 'embed': 198, 'judge': 19, 'embed_query': 19, 'answer': 19}；回答生成尝试19次，抽取尝试明细记录31次，配置内传输重试1次；已记录的回答输出截断尝试0次，抽取输出截断尝试0次。另有1次抽取调用缺少尝试明细；失败批次没有返回完整WriteReport，不能把缺失记录当作成功。

审计状态：{'bindings_checked': 10, 'bindings_incomplete': 9, 'absent': 1}；diagnostic_only交付19，宿主改写正文0。审计未完整但判分正确8题，审计绑定检查通过但答错0题；绑定检查本身不证明事实语义正确。

当前源码：`7ff0fd6c59ecc6ed8daf0c7febc8ac560a2e8f617fe640a768f376ada5f7caaa`；运行配置：`65f281a380a4a67d7643a0c3bff6f42409ed1a89a932f6bcf4a92b80b2c6971c`。源码与部署记录一致，运行配置、执行器和评分规则与上一轮相同。历史、原题和评分输入逐字节一致。耗时57.7分钟。

临时集合已全部删除；850个既有记录文件及既有集合payload哈希核验通过。运行中未改生产代码、原题、参考答案、评分标签，未根据得分重答。

按原始调用时间核对，全题回答结束后才启动评分：True。本组架构流程检查全部通过：False，逐项布尔结果保留在evaluation-summary.json。最大回答输出558 token，最大抽取输出16087 token；输出长度统计不代表抽取内容语义完整。

逐题存储审计合计：{'corpora': 20, 'facts': 1055, 'entities': 279, 'relations': 572, 'orphan_relation_fact_ids': 0}。关系引用有效只代表已存索引的一致性，不能找回未抽取的信息。

本轮与上轮的观测用量：{'baseline': {'prompt_tokens': 778342, 'completion_tokens': 205359, 'elapsed_s': 2173.161}, 'current': {'prompt_tokens': 829383, 'completion_tokens': 260339, 'elapsed_s': 3463.226}}。用量包含既有评测阶段与本地嵌入，货币价格未配置；单次运行的总耗时受模型服务和本机处理影响，不能单独视为版本速度的因果对照。

## 结论边界

本轮可验证这20个样本上的完整写入、事实召回、答案交付与审计解耦是否正常，并提供修改方向的配对证据。流程成功不等于抽取语义完整；正确率上升也不能掩盖逐题回退。

题集已经暴露，是开发回归；历史按既有Oracle设置提供，样本较少，评分沿用同一模型家族且存在随机性。不能据此证明整体架构在任意历史或新问题上正确，也不能证明已经消除通用化和信息漏存。

## 原始记录

- [结果及架构指标](evaluation-summary.json)；[全部配对结果](paired-comparison.json)
- [全部新生成回答](LongMemEval-answers.jsonl)；[原始评分](LongMemEval-grades.jsonl)
- [冻结与方法](freeze.json)；[预先确定的验收口径](assessment-plan.json)；[原始输入一致性](phase-proof.json)
- [流程失败诊断](pipeline-failure-diagnostics.json)；[内容观察及实际提示核对](content-observations.json)
- [执行与清理](execution.json)；[最终完整性核验](final-integrity.json)
