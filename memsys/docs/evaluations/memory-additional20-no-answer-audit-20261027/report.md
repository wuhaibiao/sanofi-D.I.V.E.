# 无回答审计架构：另一组20题评测

结果：**19/20判分正确，20/20正常交付**。

本组基础题号与紧邻上一组20题完全不重叠；题型配比相同，采用预先固定种子按元数据哈希选择。全部题号存在更早历史记录，因此属于开发回归，不能称为未使用盲测。

| 题型 | 正确 | 交付 | 总数 |
|---|---:|---:|---:|
| single-session-user | 3 | 3 | 3 |
| single-session-assistant | 2 | 3 | 3 |
| single-session-preference | 3 | 3 | 3 |
| knowledge-update | 3 | 3 | 3 |
| multi-session | 4 | 4 | 4 |
| temporal-reasoning | 4 | 4 | 4 |

信息不足子集：4/4。答题流程错误0，评分错误0。

| 题号 | 题型 | 交付 | 原始判分 |
|---|---|---|---|
| lme_f4f1d8a4_abs | single-session-user | answered | 正确 |
| lme_1faac195 | single-session-user | answered | 正确 |
| lme_6f9b354f | single-session-user | answered | 正确 |
| lme_561fabcd | single-session-assistant | answered | 错误 |
| lme_1903aded | single-session-assistant | answered | 正确 |
| lme_778164c6 | single-session-assistant | answered | 正确 |
| lme_0edc2aef | single-session-preference | answered | 正确 |
| lme_afdc33df | single-session-preference | answered | 正确 |
| lme_195a1a1b | single-session-preference | answered | 正确 |
| lme_031748ae_abs | knowledge-update | answered | 正确 |
| lme_10e09553 | knowledge-update | answered | 正确 |
| lme_8fb83627 | knowledge-update | answered | 正确 |
| lme_ba358f49_abs | multi-session | answered | 正确 |
| lme_gpt4_f2262a51 | multi-session | answered | 正确 |
| lme_3a704032 | multi-session | answered | 正确 |
| lme_1c549ce4 | multi-session | answered | 正确 |
| lme_gpt4_fe651585_abs | temporal-reasoning | answered | 正确 |
| lme_982b5123 | temporal-reasoning | answered | 正确 |
| lme_gpt4_4ef30696 | temporal-reasoning | answered | 正确 |
| lme_gpt4_93159ced | temporal-reasoning | answered | 正确 |

## 流程记录

历史会话33段，原始消息397条，完成处理397条；原文逐字一致：True。失败来源会话0，有隔离或失败记录的会话0。

实际调用{'extract': 33, 'embed': 207, 'judge': 20, 'embed_query': 20, 'answer': 20}；传输重试6次，提供方输出触顶或未完成0条。保留全部响应，不按得分重答。

新增回答审计字段0，可见审计信封0。20条交付正文与提供方最终返回一致。全部答题固定后才开始评分：True。

原文摘录0，待处理原文0。关系失效引用0。流程核对全部通过：True。

旧记录740个文件及既有集合payload核验一致，临时集合已清理。代码和运行配置保持冻结。耗时80.7分钟。

## 内容观察与结论边界

- lme_561fabcd：最终命名题回答为无法确认，正确名称为Fissionator。原文包含用户认可该名称及后续多次直接使用；抽取已保留认可名称与继续设计的事实。已存28条事实全部召回，其中21条含该名称；实际提供方system提示也包含这些事实，名称出现47次。回答却只采用早先的Contaminated Colossus偏好并声称没有最后选择。可确认的断点是生成阶段没有正确使用已提供的上下文，不是名称漏存、未召回、输出截断或硬校验拦截。日志不能进一步证明模型内部忽略这些事实的原因。
- lme_0edc2aef：酒店建议引用了用户对特色酒店的偏好，并以屋顶泳池连接具体建议。该题原始评分通过；这里只核对个性化上下文应用，不另行确认现实酒店设施、房价或可订状态。
- lme_afdc33df：厨房清洁建议具体使用了已有餐具收纳架、花岗岩台面及水龙头漏水的信息，并提供对应步骤。本题没有把其他烹饪话题强行当作清洁目标。
- lme_195a1a1b：晚间活动建议使用了21:30前放松的习惯，以及此前计划尝试的Body Scan冥想，未将该计划说成已经完成。本题的通过不证明旧题的腰果酸奶目标应用问题已解决。

上一组为20/20。两组题目不同，该数字仅提供上下文，不能计算配对改善或回退。

这20题使用Oracle提供的相关历史条件。样本小、题目有历史暴露、评分与回答属于同一配置模型家族；独立评分阶段不等于第三方评测。本轮可验证具体流程和这组题的表现，不能证明整个架构已正确或个性化问题已修复。

## 原始记录

- [全部指标](evaluation-summary.json)；[逐题结果](question-results.json)
- [实际回答](LongMemEval-answers.jsonl)；[原始评分](LongMemEval-grades.jsonl)
- [提供方正文一致性](provider-response-integrity.json)；[原始回答响应](LongMemEval-read-responses.jsonl)
- [预先固定的选题计划](selection-plan.json)；[去重证明](selection-proof.json)；[历史暴露记录](historical-exposure.json)
- [冻结记录](freeze.json)；[执行与清理](execution.json)；[数据完整性](final-integrity.json)
