# 当前诊断模式的20题同题回归

交付流程已恢复，总分恢复到原先正常版本，但仍有2题相对原版本退步；当前方向得到有限支持。

本次 18/20 正确，20/20 交付，流程失败 0，评分异常 0。

| 版本 | 同20题正确 | 交付完成 | 流程失败 |
|---|---:|---:|---:|
| 原先正常版本 | 18/20 | 20/20 | 0 |
| 引入硬校验后 | 14/20 | 14/20 | 6 |
| 当前仅诊断版本 | 18/20 | 20/20 | 0 |

| 对照 | 改善 | 退步 | 保持正确 | 保持错误 |
|---|---:|---:|---:|---:|
| 相对原先正常版本 | 2 | 2 | 16 | 0 |
| 相对硬校验版本 | 5 | 1 | 13 | 1 |

| 题型 | 原正常版正确 | 硬校验版正确 | 本次正确 | 本次交付 |
|---|---:|---:|---:|---:|
| 用户信息 | 3/3 | 3/3 | 3/3 | 3/3 |
| 助手信息 | 1/3 | 3/3 | 2/3 | 3/3 |
| 偏好 | 3/3 | 1/3 | 2/3 | 3/3 |
| 知识更新 | 3/3 | 2/3 | 3/3 | 3/3 |
| 多会话 | 4/4 | 2/4 | 4/4 | 4/4 |
| 时间推理 | 4/4 | 3/4 | 4/4 | 4/4 |

拒答题：4/4。

证据绑定未完整验证 8 题，其中 8 题判分正确。通过字段绑定审计但判分错误 2 题。诊断模式交付 20 题；程序改写正文 0 题。字段和引用诊断不代表语义正确性判定。

从历史重新写入 32 段、364 条消息；未完整入库会话 0，原消息逐条一致：True。每题独立建库、答题并验证清理后进入下一题，全部答完后独立评分。

账本调用记录：{'judge': 20, 'embed_query': 20, 'answer': 20, 'extract': 32, 'embed': 196}；配置内传输重试 0 次。有界输出扩容与传输重试按冻结配置执行，未按得分补答。账本计成功返回的客户端操作；失败传输的缺失用量及货币费用不作推算。

核对旧记录 2218 个文件哈希未变；源码、配置和阶段记录未变。临时集合剩余 0；原有集合保留。本地既有数据指纹一致：True。

同一20题由原30题的题型及固定哈希选出，保留各可用题型的一道拒答题，选题未读取对错标签。写入、答题、评分输入物理分离，原题、原历史和参考标签未改。评分函数、评分提示与模型及推理档位一致；原先正常版本的完整适配器文件不同，函数一致性已核对。当前输出预算、超时、长输入嵌入策略和源码较历史版本存在差异，明细见 freeze.json 与 paired-comparison.json。

这是已暴露样本的开发回归，且重新抽取和生成存在随机性。每题相当于5个百分点；小样本同模型家族评分不能证明泛化、排除所有回退或单独归因于硬规则移除。本轮没有修改产品，也未自动增加其他题集或实验。

| 题号 | 题型 | 原正常版 | 硬校验版 | 本次 | 交付状态 | 审计状态 |
|---|---|---|---|---|---|---|
| lme_bc8a6e93_abs | 用户信息 | 正确 | 正确 | 正确 | answered | bindings_checked |
| lme_25e5aa4f | 用户信息 | 正确 | 正确 | 正确 | answered | bindings_checked |
| lme_caf9ead2 | 用户信息 | 正确 | 正确 | 正确 | answered | bindings_checked |
| lme_1d4da289 | 助手信息 | 错误 | 正确 | 正确 | answered | bindings_incomplete |
| lme_6ae235be | 助手信息 | 错误 | 正确 | 正确 | answered | bindings_incomplete |
| lme_4388e9dd | 助手信息 | 正确 | 正确 | 错误 | answered | bindings_checked |
| lme_505af2f5 | 偏好 | 正确 | 正确 | 正确 | answered | bindings_checked |
| lme_caf03d32 | 偏好 | 正确 | 错误 | 错误 | answered | bindings_checked |
| lme_1da05512 | 偏好 | 正确 | 错误 | 正确 | answered | bindings_incomplete |
| lme_0ddfec37_abs | 知识更新 | 正确 | 正确 | 正确 | answered | bindings_checked |
| lme_852ce960 | 知识更新 | 正确 | 正确 | 正确 | answered | bindings_checked |
| lme_1cea1afa | 知识更新 | 正确 | 错误 | 正确 | answered | bindings_checked |
| lme_e5ba910e_abs | 多会话 | 正确 | 正确 | 正确 | answered | bindings_checked |
| lme_eeda8a6d | 多会话 | 正确 | 正确 | 正确 | answered | bindings_incomplete |
| lme_gpt4_a56e767c | 多会话 | 正确 | 错误 | 正确 | answered | bindings_incomplete |
| lme_d6062bb9 | 多会话 | 正确 | 错误 | 正确 | answered | bindings_incomplete |
| lme_gpt4_70e84552_abs | 时间推理 | 正确 | 正确 | 正确 | answered | bindings_checked |
| lme_d01c6aa8 | 时间推理 | 正确 | 正确 | 正确 | answered | bindings_incomplete |
| lme_2ebe6c90 | 时间推理 | 正确 | 错误 | 正确 | answered | bindings_incomplete |
| lme_gpt4_0a05b494 | 时间推理 | 正确 | 正确 | 正确 | answered | bindings_checked |

源码 SHA-256：12f8bce2700d79ae80c178cba41b490f863865502b213e3764b8b9682deea924。

原始回答：LongMemEval-answers.jsonl；评分：LongMemEval-grades.jsonl；同题变化：paired-comparison.json；诊断与统计：targeted-summary.json；完整性：final-integrity.json。
