# 事实选择实验：固定20题回归评测

提交 `cbaf3a086a9b72a3daad692aa8d2294da21c0571`，实验配置 `selection_policy=semantic`。

原始语义评分：18/20；回答交付：20/20；已评分：20/20。

相同题目的既有记录为 19/20；本轮改善 1 题、回退 2 题。

All 20 questions have historical answer records. Previously exposed development regression, not a new blind test.
题目、历史和独立评分输入与上一轮逐字节相同；全部从历史重新建库并真实生成。每题清理并验证为空后再进行下一题，所有回答结束后才读取评分输入。没有重放旧响应、选择性重答、改题或改写标签。

## 分题型结果

| 题型 | 正确 | 回答交付 | 已评分 |
|---|---:|---:|---:|
| single-session-user | 2/3 | 3/3 | 3/3 |
| single-session-assistant | 3/3 | 3/3 | 3/3 |
| single-session-preference | 3/3 | 3/3 | 3/3 |
| knowledge-update | 3/3 | 3/3 | 3/3 |
| multi-session | 3/4 | 4/4 | 4/4 |
| temporal-reasoning | 4/4 | 4/4 | 4/4 |

## 筛选与成本

| 指标 | 既有记录 | 本轮 |
|---|---:|---:|
| 候选事实总条数 | 781.00 | 792.00 |
| 实际入提示总条数 | 759.00 | 191.00 |
| 平均记忆正文 UTF-8 字节 | 34490.95 | 11264.70 |

本轮选择逻辑调用 20 次，降级 5 题；有未审阅候选 6 题，有预算遗漏 0 题。

筛选路径的原始评分（按运行结果分组，不能当作随机对照）：

| 路径 | 本轮正确 | 同题既有正确 | 交付 |
|---|---:|---:|---:|
| semantic_response | 13/15 | 15/15 | 15/15 |
| ranked_fallback | 5/5 | 4/5 | 5/5 |

任务关联扩展涉及 0 题，增加 0 条已存事实；没有读取历史原文作回答回退。

选择成功响应的延迟分布：{"total": 159.17149387393147, "mean": 10.611432924928765, "median": 10.071690291166306, "minimum": 4.378001667093486, "maximum": 26.26605362491682}；成功调用输入/输出 token 合计：{'input': 141157, 'output': 3645}。失败请求的消耗不完整记入成功账本，不能将这些数字作为全部失败调用的费用。

读取阶段的成功调用输入 token：原回答 186117 → 本轮回答加筛选 224636，增加 20.70%。回答上下文变短没有转化为总输入 token 降低；这不是货币费用估计，也未包含失败请求未知的消耗。

本轮成功交付题的平均读取加答题耗时为 38.5669 秒，既有记录为 50.23865 秒；包含筛选等读取工作，不含写入和评分。

成功账本中的传输重试 7 次。原始服务异常按用途：{'fact_select': 5}。

抽取协议：{'partial_sessions': 1, 'rejected_entries': 8, 'repaired_entries': 0}。回答错误 0 题，评分错误 0 题；原文摘录 0、临时原文 0。

条数是候选和装箱统计，不是语义召回率、精确率或逐条实际使用率。两轮写入均有随机性，事实池也可能变化；上下文减少不能全部归因于选择器。价格未完整配置时货币费用未知。

## 同题变化及未通过项

### lme_1faac195 · single-session-user

旧判定：True；本轮原判定：False；变化：regressed。

**原题：** Where does my sister Emily live?

**参考答案（仅评分阶段使用）：** Denver

**原始回答：** I know Emily was associated with Denver in the context of your planned visit, but I don’t know whether she lives there.

### lme_561fabcd · single-session-assistant

旧判定：False；本轮原判定：True；变化：improved。

**原题：** I was thinking back to our previous conversation about the Radiation Amplified zombie, and I was wondering if you remembered what we finally decided to name it?

**参考答案（仅评分阶段使用）：** Fissionator.

**原始回答：** I don’t have a final choice recorded. You liked **“Contaminated Colossus”** and asked for smoother one-word alternatives; **Radik**, **Irradon**, and **Fissionator** were among the suggestions, but I can’t tell which—if any—you settled on.

### lme_gpt4_f2262a51 · multi-session

旧判定：True；本轮原判定：False；变化：regressed。

**原题：** How many different doctors did I visit?

**参考答案（仅评分阶段使用）：** I visited three different doctors: a primary care physician, an ENT specialist, and a dermatologist.

**原始回答：** You mentioned **3 different doctors**: Dr. Lee, Dr. Smith, and Dr. Patel. The records explicitly say you attended an appointment with Dr. Lee; they don’t confirm whether you physically visited Dr. Smith or Dr. Patel.

## 全部答案固定后的核查

本轮证明了记忆正文压缩和整题交付路径可以工作，但没有证明准确性改善。原评分从19/20降为18/20；唯一判为改善的题仍未明确回答最终选择，存在误判通过，不能当作真实修复收益。实验应继续留在分支。

以下是原始评分之外的定性核查，未覆盖或改写机器评分标签。

- 第 2 题：12个候选全部送入筛选，人物关系和拜访地点两条关键事实均入选；没有传输降级、未审阅候选或装箱预算遗漏。原文是计划去某城市拜访姐姐，没有直述长期居住地；数据参考答案采用正常语境推断。回答明知城市却拒绝确认居住地，属于生成过度保守，不能归因为Top-K不足或证据校验拦截。证据见该题write-inputs、write记录、trace和原始回答。
- 第 14 题：三名医生及其身份均进入实际回答提示，但抽取将原文明确说见过耳鼻喉医生的经历概括为诊断事实，未单独保留就诊事件；回答给出3人后又否认其中两人的实际就诊得到确认，因此被判错。该题90个候选只审阅44个，选择输入65457 UTF-8字节接近65536上限，另46个未审阅，输入限制确实被触及；当前证据不足以认定未审阅部分就是致错的缺失前提。选中3条均完整装入回答，不能说是回答输出截断或全部由选择器误删造成。
- 第 4 题：原判从错误变为正确，但回答仍声明没有最终选择，只把参考名称列为多个候选之一。这是评分可能误判通过的具体例子。该题筛选调用因SSL提前关闭而降级，29条候选全部装入；不能把该项原判改善归功于正常语义选择。原始布尔标签保留，未重判或补答。
- 第 11 题：第二段历史抽取输出含3个事实对象及8个字段名字符串，8个字符串以fact_not_object拒收。响应正常stop结束，1044输出token，低于65536上限；这是模型输出形状异常而非触顶截断。有效对象和其余历史继续入库，该题原判通过。拒收的字段名本身不包含8条完整事实，不能把拒收计数等同于8条语义事实损失。
- 5/20题筛选调用出现SSL UNEXPECTED_EOF_WHILE_READING，均回退并交付；正常筛选响应的15题原判13/15，相同题旧记录15/15；降级组5/5，旧记录4/5。按实际路径分组并非随机对照，无法单独估计选择器净贡献，也无法仅凭此SSL症状区分本地网络、中间代理或服务端原因。
- 6题存在未审阅候选，0题记录选中事实装箱遗漏；任务关联扩展0次。本轮没有验证新任务扩展的效果，也不能将选择器自评complete视为真实语义覆盖率。

已核看20题原始回答与参考答案，并针对两项回退、原判改善和格式异常追查源历史、写入及选择记录。这是定性核查，不是另一轮评分或新盲测，不输出改写后的官方准确率。重新抽取与生成的随机性、同模型系列评分、Oracle历史条件和小样本均限制解释；本轮不据公开错题修改产品或选择性重答。

## 完整性与解释边界

{
  "baseline_files_unchanged": true,
  "changed_baseline_files": [],
  "unrelated_payloads_unchanged": true,
  "source_unchanged": true,
  "runtime_configuration_unchanged": true,
  "remaining_owned_collections": []
}

这轮是已暴露题目的小样本开发回归；Oracle 历史条件、重新抽取与生成的随机性、同模型系列评分均限制解释。分数变化不能证明架构泛化正确性，也不能证明每条选中事实都被使用。原始失败保留，不在这一轮内据错题修改产品或补答。

[逐题完整结果](question-results.json) · [汇总](evaluation-summary.json) · [冻结记录](freeze.json) · [执行和清理](execution.json)

最终源码 SHA256：`a45d9af84c13d91a4a3de3d900be3b8a93850afb0370cc8a58e16d55ef509cc5`。
