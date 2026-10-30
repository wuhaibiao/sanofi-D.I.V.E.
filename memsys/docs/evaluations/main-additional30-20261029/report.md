# 恢复主干后的另一组 30 题评测

结果：**24/30 原始判分正确（80.0%），30/30 正常交付**。

源码、配置和评测入口与先前主干一致。每题从空隔离库重新写入相关历史，使用事实召回及真实回答；全部回答固定后再统一评分。

与前两组主干共 40 道基础题及拒答变体无重叠；本组 30/30 道查到更早的题号元数据，属于开发评测。选题仅按预先冻结的种子、题型和拒答标记进行。

| 题型 | 正确 | 交付 | 总数 |
|---|---:|---:|---:|
| single-session-user | 4 | 5 | 5 |
| single-session-assistant | 5 | 5 | 5 |
| single-session-preference | 4 | 5 | 5 |
| knowledge-update | 5 | 5 | 5 |
| multi-session | 3 | 5 | 5 |
| temporal-reasoning | 3 | 5 | 5 |

信息不足题 4/4；答题流程错误 0，评分调用失败 0。

## 用量与耗时

| 环节 | 成功记录调用 | 输入 token | 输出 token | 平均远端请求秒 |
|---|---:|---:|---:|---:|
| extract | 48 | 345977 | 369901 | 50.51 |
| answer | 30 | 282763 | 5158 | 4.61 |
| judge | 30 | 5670 | 910 | 2.87 |

生产 LLM 合计 78 次，输入 628740、输出 375059 token；评分另计。本地 embedding 和 BGE 单独统计，未算作远端 LLM 调用。

传输重试 0 次，提供方错误事件 0 次，输出触顶或未完成响应 0 条。失败尝试未返回的用量无法完整统计；价格未配置，货币成本未知。

运行总耗时 76.0 分钟；平均读取＋答题 21.55 秒。
平均回答输入 9425 token；远端回答平均 4.61 秒。
平均装入事实 38.03 条；平均 memory 33930 字节；BGE 平均 14.91 秒。
平均传给宿主的记忆及使用规则 39858 字节。字节与模型 token 分别统计。

## 原始逐题结果

| 题号 | 题型 | 交付 | 原始判分 |
|---|---|---|---|
| lme_0862e8bf_abs | single-session-user | answered | 正确 |
| lme_c8c3f81d | single-session-user | answered | 正确 |
| lme_ccb36322 | single-session-user | answered | 正确 |
| lme_a06e4cfe | single-session-user | answered | 正确 |
| lme_58ef2f1c | single-session-user | answered | 错误 |
| lme_4c36ccef | single-session-assistant | answered | 正确 |
| lme_eaca4986 | single-session-assistant | answered | 正确 |
| lme_3e321797 | single-session-assistant | answered | 正确 |
| lme_7161e7e2 | single-session-assistant | answered | 正确 |
| lme_ceb54acb | single-session-assistant | answered | 正确 |
| lme_35a27287 | single-session-preference | answered | 正确 |
| lme_b6025781 | single-session-preference | answered | 错误 |
| lme_32260d93 | single-session-preference | answered | 正确 |
| lme_09d032c9 | single-session-preference | answered | 正确 |
| lme_38146c39 | single-session-preference | answered | 正确 |
| lme_6aeb4375_abs | knowledge-update | answered | 正确 |
| lme_d7c942c3 | knowledge-update | answered | 正确 |
| lme_3ba21379 | knowledge-update | answered | 正确 |
| lme_4b24c848 | knowledge-update | answered | 正确 |
| lme_50635ada | knowledge-update | answered | 正确 |
| lme_2311e44b_abs | multi-session | answered | 正确 |
| lme_21d02d0d | multi-session | answered | 错误 |
| lme_c4a1ceb8 | multi-session | answered | 错误 |
| lme_3fdac837 | multi-session | answered | 正确 |
| lme_b5ef892d | multi-session | answered | 正确 |
| lme_gpt4_c27434e8_abs | temporal-reasoning | answered | 正确 |
| lme_gpt4_e072b769 | temporal-reasoning | answered | 错误 |
| lme_370a8ff4 | temporal-reasoning | answered | 错误 |
| lme_e4e14d04 | temporal-reasoning | answered | 正确 |
| lme_gpt4_7ca326fa | temporal-reasoning | answered | 正确 |

## 内容观察

- lme_58ef2f1c：原始历史明确说在情人节当天参加；写入的一条事件只保留到二月，另一条偏好事实保留了情人节文字且已进入回答提示。回答仅答二月，没有利用更细的时间。不能归因于完全未召回。
- lme_b6025781：该题28条已存事实全部装入。回答确实使用藜麦、烤蔬菜、鸡肉和火鸡经历、迷迭香，因此不能笼统认定为通用化；但重复了旧的肉酱建议，未延续已选定的甜薯方案与自制格兰诺拉目标。原始标签为错误，仅凭该标签无法确定评分模型的具体判错理由。
- lme_21d02d0d：历史把工作繁忙与错过3月26日活动放在同一语境；写入只保留错过该日活动，未保留工作原因。3月5日的工作原因有保留。81条已存事实全部召回，回答因缺少3月26日的原因而只计1次并保留不确定性。主要断点在抽取的原因关联。
- lme_c4a1ceb8：已完成的桑格利亚酒经历明确含橙片和柠檬片，并且进入实际回答提示；另有青柠鸡尾酒经历。回答却只计橙和青柠，遗漏柠檬，还说只有青柠明确用于完成的饮品。已知关键证据并非被Top-K截掉。
- lme_gpt4_e072b769：原始历史在4月16日说刚下载Ibotta；39条已存事实未保留这次下载或开始使用的起始事件，只保留购物目标、返现习惯等。回答仅能说截至4月16日在用，无法计算已用几周。主要断点在写入，不能仅增加召回条数解决。
- lme_370a8ff4：历史给出的流感恢复日为2023-01-19，第10次户外慢跑日为2023-04-10；这两条事实均进入提示。两日相差81天，即11周4天，与实际回答一致，但原始评分为错误。未读取参考答案来推定评分理由，未重评分、未改标签；正式成绩仍为24/30。

## 完整性与解释范围

历史 48 段、原始消息 544 条，完成处理 544 条；写入失败会话 0，原文逐字一致 True。

事实 1651 条，失效关系引用 0 条；原文摘录与待处理原文进入回答均为 0。全部回答完成后评分 True；交付正文与提供方最终返回一致 30/30。

源码、配置、旧记录保持冻结；临时集合已清理。既有集合内容一致 True；流程核对全部通过 True。只读存储核对不参与答题，也不增加模型调用。

原始错误、响应与标签全部保留，没有按得分重答或替换。以上是这组小样本的真实运行结果，不能据此认定架构完整正确；Oracle 已提供相关历史，不能替代完整长历史检索评测。同一配置模型家族评分可能误判。

前两组主干历史结果为 20/20、19/20。本轮题目不同，只能提供背景，不能作为配对改善或回退，也不能把跨轮延迟差异归因于架构。

## 记录

- [全部指标](evaluation-summary.json)；[逐题回答与状态](question-results.json)
- [实际回答](LongMemEval-answers.jsonl)；[原始评分](LongMemEval-grades.jsonl)
- [冻结记录](freeze.json)；[选择规则](selection-plan.json)；[选择证明](selection-proof.json)
- [执行记录](execution.json)；[数据完整性](final-integrity.json)；[提供方正文核对](provider-response-integrity.json)

源码 SHA-256：73d20a5fd02f6800e73b0f01a7b45a4b93d7c36573cf3302b058e935a72e55f6
