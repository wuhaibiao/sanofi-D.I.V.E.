# 主干来源优先与 Top-20：同一批20题复测

分支 `codex/memory-top20-refinement`，最终提交 `67b13488063eb61452b6c144fa26af8028dc4c16`。

原始评分 **18/20**，主干既有记录 **19/20**；交付 20/20，有效评分 20/20；回答错误 0，评分错误 0。

本轮确认了上下文与LLM token用量下降，但尚未证明质量改良。原始18/20低于主干旧记录19/20，与上次Top-10实验18/20相同；20题均交付。上次两道回退题本轮恢复，但出现职位身份/初始时点匹配错误，且评分存在假阴性和假阳性疑点。保留实验分支，不建议切换主干默认路径。

本轮从主干当前工作状态的快照开分支。保留本地 BGE，把同源结构尽量完整保存，保留旧来源优先与个性化分组，在优先级内按明确身份轮流选取，最终最多20条，初始正文预算65536 UTF-8字节，沿用宿主窗口内的预算扩展且事实仍最多20条，关闭额外模型筛选及回答审计。主干配置和服务未切换。

同一批原题按原顺序重新写入、真实回答；每题清理独立集合，全部答案固定后才评分。与主干旧记录对照，没有新跑一轮基线，没有重放答案或选择性补答。

对照条件：旧评测保留的 76 个 Python 源码文件与本次主干起点逐字节一致，模型等非 memory 配置一致。两轮实际调用发生在不同时间，仍有生成、抽取、服务状态及评分随机性。

## 正确率与交付

| 题型 | 主干旧记录 | 本轮 | 交付 |
|---|---:|---:|---:|
| single-session-user | 3/3 | 3/3 | 3/3 |
| single-session-assistant | 2/3 | 3/3 | 3/3 |
| single-session-preference | 3/3 | 2/3 | 3/3 |
| knowledge-update | 3/3 | 2/3 | 3/3 |
| multi-session | 4/4 | 4/4 | 4/4 |
| temporal-reasoning | 4/4 | 4/4 | 4/4 |

同题变化：{"unchanged": 17, "improved": 1, "regressed": 2}。

上一个 Top-10 实验分支的同题原始评分为 18/20、交付 20/20；该轮的输入、调用、存储与耗时也单独保存在汇总的 prior_experiment 中。三轮在不同时间调用，不能据此分离每项改动的因果贡献。

## 体积、调用与时间

| 指标 | 主干旧记录 | 本轮 | 变化 |
|---|---:|---:|---:|
| 已存事实总条数 | 1,137.00 | 968.00 | -14.86% |
| 事实正文加值 UTF-8 字节 | 724,161.00 | 697,409.00 | -3.69% |
| 其中助手事实 | 851.00 | 700.00 | -17.74% |
| 入提示事实总条数 | 759.00 | 317.00 | -58.23% |
| 平均记忆正文 UTF-8 字节 | 34,490.95 | 13,865.30 | -59.80% |
| 回答输入 token | 186,117.00 | 90,442.00 | -51.41% |
| 抽取输出 token | 294,342.00 | 246,941.00 | -16.10% |
| 写入＋回答 LLM 输入 token | 432,194.00 | 360,213.00 | -16.65% |
| 写入＋回答 LLM 输出 token | 297,192.00 | 250,054.00 | -15.86% |
| 写入＋回答 LLM 成功调用 | 53.00 | 53.00 | +0.00% |
| 平均读取＋回答耗时（秒） | 50.24 | 19.37 | -61.45% |
| 写入＋读取阶段总耗时（秒） | 4,700.06 | 2,989.80 | -36.39% |

生产调用的分项（成功响应账本，评分费用单列）：

| 用途 | 旧调用/新调用 | 旧输入/新输入 token | 旧输出/新输出 token |
|---|---:|---:|---:|
| answer | 20 / 20 | 186,117 / 90,442 | 2,850 / 3,113 |
| embed | 207 / 196 | 408,950 / 409,458 | 0 / 0 |
| embed_query | 20 / 20 | 400 / 400 | 0 / 0 |
| extract | 33 / 33 | 246,077 / 269,771 | 294,342 / 246,941 |
| judge | 20 / 20 | 3,755 / 3,728 | 477 / 818 |

本轮额外模型筛选 0 次；BGE 降级 0 题；覆盖补取扩展 7 题。最终条数是上限，不要求填满。

成功账本的传输重试：旧 6 次，新 0 次；本轮原始调用异常 0 次。

费用没有完整单价时记为未知，不能把账本中的零当作免费，也不能把 token 降幅直接写成货币节省。未返回用量的失败请求可能不在成功账本内。BGE 与 embedding 的本地计算另有资源消耗；本次每题独立进程，读取耗时含模型冷加载。

## 写入、选择与完整性

源会话 33 个，消息 397 条；原文完全一致 True。未完成源会话 0，部分有效抽取协议 1 个会话。

写入计数：`{"extract_calls": 33, "extract_model_facts": 570, "extract_rejected_count": 1, "extract_repaired_count": 0, "literal_structures": 407, "literal_structures_before_compaction": 462, "literal_structures_collapsed": 55, "literal_structures_skipped": 0, "covered_literal_structures": 9, "duplicates": 0, "relation_edges": 615}`。

这些是结构体积和装入数量，不能据此声称语义召回率、精确率、去重准确率或实际逐条使用率。紧凑记录变大后也可能挤占预算；Top-20 也可能丢掉低排名的必要事实。

## 全部答案固定后的核查

本轮确认了上下文与LLM token用量下降，但尚未证明质量改良。原始18/20低于主干旧记录19/20，与上次Top-10实验18/20相同；20题均交付。上次两道回退题本轮恢复，但出现职位身份/初始时点匹配错误，且评分存在假阴性和假阳性疑点。保留实验分支，不建议切换主干默认路径。

- 实现保持主干原有ranked入口、BGE、来源优先及建议分组，再在优先级内按明确对象/事件轮流装入。20题最终最多20条，平均15.85条；20题都经过本地多样性处理，未增加生成式选择或画像维护调用。候选池深度沿用基线；这不是只改变K的一项消融。
- 第 9 题：上次Top-10失败的晚间活动题本轮恢复：用户9:30前休息的偏好被选入，实际回答以该时间和睡前放松为建议依据；原始判True。只能确认本轮行为恢复，不能单独量化K增大或来源排序的贡献。
- 第 14 题：上次Top-10失败的医生计数题本轮恢复：ENT、皮肤科与既往家庭医生就诊事实均选入20条，回答3位，并排除了仅计划的随访；原始判True。
- 第 10 题：职位题是真实错答。原始用户报告的新职位是Senior Software Engineer，题目问Software Engineer Manager，缺少该身份对应信息。提示中同时包含初始4人和后续5人的明确用户事实，最终回答却报5人并附上职位纠正；没有正确处理所问职位与初始时点。必要事实已存且已进入实际提示，主要断点在回答对问题身份/阶段的适配，不能归咎于Top-20截掉事实；本轮这道拒答题False，拒答类原始通过3/4。
- 第 8 题：厨房题原始判False，但疑似评分假阴性。新置物架、保持台面整洁、靠近水槽的花岗岩划痕及轻微漏水均有用户原文、已存并已选入；回答具体给出置物架收纳、软布擦拭花岗岩、防止增加划痕和处理水槽/漏水的建议，与参考要求及评分提示的个性化标准直接对应。模型仅返回no，没有拒绝理由，无法断言其内部判断原因。保留False，不把它直接诊断为又一次通用化。
- 第 7 题：酒店题原始判True，但个性化仍有不足。城市景观、屋顶泳池/阳台热水浴缸等十条用户背景均进入提示，回答却主要以海滩、餐饮和首次去Miami为依据，未体现已知景观和特色设施偏好，也未说明推荐是否满足它们；给定历史没有首次去Miami的用户陈述。不能从黑盒判断模型内部是否考虑过偏好，但最终文本没有稳定体现这些偏好。通过标签不代表此问题已经解决。
- 第 4 题：命名题唯一的原始改善仍不能视为已证实改善。模型答没有最终决定记录、只是Fissionator为领先选择，却被判True。原文后续多次沿用这个名称，且对应后续设计事实已选入20条；本轮仍存在过度保守与评分假阳性疑点。保留原始True，不另算人工总分。
- 第 12 题：一次部分协议异常来自多分区表格：模型输出合法JSON的type=table、sections数组，每个section各有columns/rows，但现有协议只接受顶层columns/rows。该1条复合表格事实因结构不兼容被隔离，属于抽取输出与解析协议不匹配，不是输出截断。三个分区的表格单元由独立字面表格兜底保存；用户已读5期的事实仍选入，回答5且原始判True。隔离未造成整批或答题失败，但不能据此保证复合事实的所有用途/关系信息均无损，独立测试尚未覆盖多分区表格表示。
- 相较主干旧记录，生产LLM输入432194→360213（-16.65%）、输出297192→250054（-15.86%）；答题输入186117→90442（-51.41%）。写入输入246077→269771反而增加9.63%，所以不能用答题输入降幅代表全流程节省。调用仍53次=33个源会话抽取+20次答题；评分20次另计，完整评测73次生成调用。所有用量均有返回，价格未知，不计算实付金额。
- 相较上次Top-10实验，本轮生产LLM输入增加10.58%、输出增加0.27%，原始分数同为18/20。Top-20恢复了两道旧回退，但不能据此说它比Top-10同时更省且更准；两轮写入和排序也有差异。
- 事实1137→968（-14.86%），助手事实851→700；字面结构462→407，减少55条表示，没有按no_new_memory声明跳过结构。570条模型事实+407条字面结构-9条已覆盖=968。文本加值724161→697409字节仅下降3.69%，条数下降不等于同幅的存储节约或已验证的语义去重准确率。
- 平均读取+答题50.24→19.37秒，但本地BGE总耗时235.26→272.01秒、embedding成功调用延迟327.56→386.64秒，均增加。BGE窗口总数仍716；调用模型响应延迟大幅变化且旧轮有6次传输重试、新轮0次。两轮运行时间不同，不能把总体加速全部归因于Top-20、上下文缩减或本地计算减少。
- 33个会话、397条消息输入字节与旧评测完全一致；20题真实重新写入、答题后再统一判分。所有73个生成响应均stop、0输出触顶、0提供方终止错误、0传输重试、BGE降级0题。测试集合与临时凭据已清理；主干HEAD/工作状态/索引、原运行配置、旧结果及无关数据库负载均未改变。

这是已暴露的小样本Oracle开发回归，不是新盲测或架构泛化证明。离线阅读全部20份答案和参考，重点核对变化题、通过但疑似泛化的回答、原文与实际提示、协议异常；没有为全部历史另建独立人工标签。原始18/20及每个True/False保持不变；不据局部复核改成19/20或任何人工分数。全部答案冻结后才判分和复核，未修改产品代码、问题、参考、标签或进行选择性重答。

### 第 4 题 · single-session-assistant

旧判定 False；新判定 True；improved。

**原题：** I was thinking back to our previous conversation about the Radiation Amplified zombie, and I was wondering if you remembered what we finally decided to name it?

**本轮回答：** I don’t have a record of a final decision, but you said you especially liked **Fissionator**—so that seems to have been the leading choice.

**参考答案（仅评分阶段使用）：** Fissionator.

### 第 8 题 · single-session-preference

旧判定 True；新判定 False；regressed。

**原题：** My kitchen's becoming a bit of a mess again. Any tips for keeping it clean?

**本轮回答：** A few small routines can keep it from building up:

- **Reset the counters after cooking:** put utensils back in your holder and wipe the granite with a soft cloth. Keeping grit off the surface also helps prevent more scratches.
- **Keep the utensil holder tidy:** group tools by use, remove duplicates you rarely reach for, and wash the holder now and then to clear out crumbs or residue.
- **Handle the sink area promptly:** dry splashes and check under the faucet—since yours has been leaking slightly, fixing that leak can prevent a recurring wet mess.
- **Do a quick evening reset:** wash or load dishes, wipe the sink and counters, and take out food scraps before they linger.

Even a five-minute reset each day can make the kitchen feel much more manageable.

**参考答案（仅评分阶段使用）：** The user would prefer responses that acknowledge and build upon their existing efforts to organize their kitchen, such as utilizing their new utensil holder to keep countertops clutter-free. They would also appreciate tips that address their concern for maintaining their granite surface, particularly around the sink area. Preferred responses would provide practical and actionable steps to maintain cleanliness, leveraging the user's current tools and setup. They might not prefer generic or vague suggestions that do not take into account their specific kitchen setup or concerns.

### 第 10 题 · knowledge-update

旧判定 True；新判定 False；regressed。

**原题：** How many engineers do I lead when I just started my new role as Software Engineer Manager?

**本轮回答：** You lead **5 engineers**. I have your role noted as Senior Software Engineer, rather than Software Engineer Manager.

**参考答案（仅评分阶段使用）：** The information provided is not enough. You mentioned starting the role as Senior Software Engineer but not Software Engineer Manager.

## 解释边界

这是已暴露的 20 题开发回归，使用 Oracle 历史条件；两轮抽取、生成和评分均有随机性，评分模型与回答模型属于同一配置系列。结果只支持这批题的观察，不能证明架构普遍正确或差异完全来自某一项改动。原始评分不会因人工核查被覆盖。

本地验证新增 22 项通过；基线与分支均有相同 2 项旧断言失败、3 项数据库鉴权前置错误。本轮扩展检查148/153项通过；未声称所有测试通过。

完整性：`{"baseline_files_unchanged": true, "changed_baseline_files": [], "unrelated_payloads_unchanged": true, "source_unchanged": true, "runtime_configuration_unchanged": true, "main_checkout": {"head_unchanged": true, "status_unchanged": true, "index_unchanged": true}, "remaining_owned_collections": []}`。

[逐题结果](question-results.json) · [汇总](evaluation-summary.json) · [冻结记录](freeze.json) · [执行与清理](execution.json)

最终源码 SHA256：`a52e73177d66837f079c8f0f48c6d89806f143eabba905518945383aa6d1ba34`。
