# 紧凑写入与 BGE Top-10：同一批20题复测

分支 `codex/compact-ranked-refactor`，最终提交 `2f94cad6a7b9de3325cbd0acc30c7a582eb7856a`。

原始评分 **18/20**，主干既有记录 **19/20**；交付 20/20，有效评分 20/20；回答错误 0，评分错误 0。

原始评分18/20，低于主干旧记录19/20。改善1题、回退2题，其余17题原判不变；20题均正常交付。上下文和token下降，但本次实现没有完整保留旧版的用户事实优先、对象覆盖排序及个性化渲染，实验存在实现回退，不能视为质量改良，也不能把退步单独归因于K=10。不建议合入主干默认路径。

本轮从主干当前工作状态的快照开分支。保留本地 BGE，把同源结构尽量完整保存，统一最终 Top-10 与 16384 UTF-8 字节记忆预算，关闭额外模型筛选及回答审计。主干配置和服务未切换。

同一批原题按原顺序重新写入、真实回答；每题清理独立集合，全部答案固定后才评分。与主干旧记录对照，没有新跑一轮基线，没有重放答案或选择性补答。

对照条件：旧评测保留的 76 个 Python 源码文件与本次主干起点逐字节一致，模型等非 memory 配置一致。两轮实际调用发生在不同时间，仍有生成、抽取、服务状态及评分随机性。

## 正确率与交付

| 题型 | 主干旧记录 | 本轮 | 交付 |
|---|---:|---:|---:|
| single-session-user | 3/3 | 3/3 | 3/3 |
| single-session-assistant | 2/3 | 3/3 | 3/3 |
| single-session-preference | 3/3 | 2/3 | 3/3 |
| knowledge-update | 3/3 | 3/3 | 3/3 |
| multi-session | 4/4 | 3/4 | 4/4 |
| temporal-reasoning | 4/4 | 4/4 | 4/4 |

同题变化：{"unchanged": 17, "improved": 1, "regressed": 2}。

## 体积、调用与时间

| 指标 | 主干旧记录 | 本轮 | 变化 |
|---|---:|---:|---:|
| 已存事实总条数 | 1,137.00 | 879.00 | -22.69% |
| 其中助手事实 | 851.00 | 620.00 | -27.14% |
| 入提示事实总条数 | 759.00 | 176.00 | -76.81% |
| 平均记忆正文 UTF-8 字节 | 34,490.95 | 10,439.30 | -69.73% |
| 回答输入 token | 186,117.00 | 74,581.00 | -59.93% |
| 抽取输出 token | 294,342.00 | 246,153.00 | -16.37% |
| 写入＋回答 LLM 输入 token | 432,194.00 | 325,763.00 | -24.63% |
| 写入＋回答 LLM 输出 token | 297,192.00 | 249,383.00 | -16.09% |
| 写入＋回答 LLM 成功调用 | 53.00 | 53.00 | +0.00% |
| 平均读取＋回答耗时（秒） | 50.24 | 21.22 | -57.77% |
| 写入＋读取阶段总耗时（秒） | 4,700.06 | 3,183.26 | -32.27% |

生产调用的分项（成功响应账本，评分费用单列）：

| 用途 | 旧调用/新调用 | 旧输入/新输入 token | 旧输出/新输出 token |
|---|---:|---:|---:|
| answer | 20 / 20 | 186,117 / 74,581 | 2,850 / 3,230 |
| embed | 207 / 192 | 408,950 / 386,394 | 0 / 0 |
| embed_query | 20 / 20 | 400 / 400 | 0 / 0 |
| extract | 33 / 33 | 246,077 / 251,182 | 294,342 / 246,153 |
| judge | 20 / 20 | 3,755 / 3,702 | 477 / 614 |

本轮额外模型筛选 0 次；BGE 降级 0 题；覆盖补取扩展 0 题。最终条数是上限，不要求填满。

成功账本的传输重试：旧 6 次，新 0 次；本轮原始调用异常 0 次。

费用没有完整单价时记为未知，不能把账本中的零当作免费，也不能把 token 降幅直接写成货币节省。未返回用量的失败请求可能不在成功账本内。BGE 与 embedding 的本地计算另有资源消耗；本次每题独立进程，读取耗时含模型冷加载。

## 写入、选择与完整性

源会话 33 个，消息 397 条；原文完全一致 True。未完成源会话 0，部分有效抽取协议 0 个会话。

写入计数：`{"extract_calls": 33, "extract_model_facts": 503, "extract_rejected_count": 0, "extract_repaired_count": 0, "literal_structures": 378, "literal_structures_before_compaction": 462, "literal_structures_collapsed": 54, "literal_structures_skipped": 30, "covered_literal_structures": 2, "duplicates": 0, "relation_edges": 503}`。

这些是结构体积和装入数量，不能据此声称语义召回率、精确率、去重准确率或实际逐条使用率。紧凑记录变大后也可能挤占预算；Top-10 也可能丢掉低排名的必要事实。

## 全部答案固定后的核查

原始评分18/20，低于主干旧记录19/20。改善1题、回退2题，其余17题原判不变；20题均正常交付。上下文和token下降，但本次实现没有完整保留旧版的用户事实优先、对象覆盖排序及个性化渲染，实验存在实现回退，不能视为质量改良，也不能把退步单独归因于K=10。不建议合入主干默认路径。

- 代码断点：read.py:579的新路径提前返回select_ranked()，绕过旧选择阶段read.py:604的order_facts()。旧函数保留用户事实/偏好优先和显式对象、事件的覆盖顺序；新ranked_selection.py:12直接按BGE分数装箱，:17固定scope=mixed。read.py:767的用户建议个性化分组只在scope=user时启用，因此新路径也没有触发该分组及其提示。这一实现改变了已有的筛选和提示行为，并非只把旧方案的K调小。运行迹象与此一致：两道回退题的Top-10都有9条助手内容，必要用户事实排18/38被截掉。但没有消融实验，不能量化各项改动的独立贡献。
- 第 9 题：晚间活动题：用户“9:30前开始休息”的明确偏好已存为事实，但BGE第18位，因最终Top-10被省略；选中的10条中9条是助手内容、1条是用户计划。没有字节预算遗漏（记忆正文11369/16384字节）。助手的睡前安排（含9:30及收起设备）和用户冥想计划仍进入实际提示，最终回答却泛化、没有应用具体时间，还建议观看轻松内容。因此既有选择阶段丢失用户约束，也有生成阶段未充分使用剩余信息。不能只归咎于漏存或上下文不够。
- 第 14 题：医生计数题：第三位医生的既往就诊/开药事实已存入库，在59个重排候选中排第38位，被Top-10截掉。10条选中事实里9条是助手内容，只有1条用户事件；正文14911/16384字节，字节预算未触发遗漏。回答报2位，参考为3位。这里是重排与装入筛选失效；事实缺失于答题输入，不能说是数据库未存。
- 第 4 题：唯一被机器判为“改善”的命名题仍回答“没有最终决定记录，无法判断选哪一个”，只是同时提到了参考名称。历史后续已持续使用该名称。原判保留True，但该回答不能视为已稳定恢复直接回答能力，存在评分假阳性嫌疑；不据人工核查改写原始机器标签。
- 第 9 题：评分参考还提到“手机/电视影响睡眠”的用户背景，但本次隔离历史的用户原话中未找到这段陈述；不能据参考把它诊断为抽取漏存。可以直接核实的个人约束是9:30前休息及睡前放松/冥想偏好。两轮参考输入相同，原始失败判定保留。
- 写入体积确有下降：事实1137→879，其中助手事实851→620。模型抽取事实678→503；确定性结构462→378，其中同源结构合并减少54条，按有效no_new_memory声明跳过30条；再扣除2条已被模型事实覆盖的结构，合计879。它们是体积计数，不能据此证明无漏存或语义去重正确。
- 本轮所有20题最多10条、正文最大16382字节；额外模型筛选、BGE降级、覆盖扩展均为0。Top-10的执行机制按设计生效，但相关性排序不等于必要用户约束或计数对象的覆盖。这批回退表明，直接收紧K不足以保证答案质量。
- 写入+答题LLM成功调用均为53次（33抽取+20回答），输入432194→325763 token，输出297192→249383 token；不含独立评分。没有减少LLM调用次数。抽取输入略升246077→251182，回答输出略升2850→3230，总输入/输出仍下降。单价未配置，不能给实付金额或直接把token降幅写成金额降幅。
- 后续应先用独立自编场景修正这次没有保留旧行为的实现问题，再研究重复助手建议、用户约束和对象覆盖在固定预算中的取舍。本轮没有按公开错题修改生产代码、换K、补答或追加消融；本轮不能用于判断“保留旧选择逻辑，仅缩减预算”的单独效果。
- 耗时不应全部归因于架构：平均读取+回答50.24→21.22秒，写入+读取阶段合计4700.06→3183.26秒，但本地BGE总耗时235.26→267.60秒，embedding成功调用合计延迟327.56→368.02秒，均增加。两轮API传输重试6→0、服务运行时段不同，模型服务速度及冷加载也影响耗时，不能把整体加速百分比直接当作稳定线上吞吐收益。

以上是在全部20题答案、机器评分固定以后进行的离线核查；未调用额外评分模型，未改标签、未补答。原始18/20不能直接替换成人工17/20：本次没有对每条事实建立独立完整标注，也没有证明其余通过题均无评分偏差。这是已暴露的小样本Oracle开发回归，不是盲测或架构泛化证明。

### 第 4 题 · single-session-assistant

旧判定 False；新判定 True；improved。

**原题：** I was thinking back to our previous conversation about the Radiation Amplified zombie, and I was wondering if you remembered what we finally decided to name it?

**本轮回答：** I don’t have a record of a final decision. You liked **Contaminated Colossus**, and **Fissionator** stood out as a favorite for the clunky, mechanical design—but I can’t tell which one you ultimately chose.

**参考答案（仅评分阶段使用）：** Fissionator.

### 第 9 题 · single-session-preference

旧判定 True；新判定 False；regressed。

**原题：** Can you suggest some activities that I can do in the evening?

**本轮回答：** Here are a few evening activities, depending on the mood you want:

- **To unwind:** read, listen to calming music, or try a short guided meditation or body scan.
- **To get some fresh air:** take a relaxed walk or do gentle stretching.
- **To feel productive:** make a simple meal, tidy one small area, or prepare for tomorrow.
- **For fun or connection:** work on a hobby, watch something light, or call a friend.

If you’re getting ready for bed, choose something calming and give yourself a little screen-free time beforehand.

**参考答案（仅评分阶段使用）：** The user would prefer suggestions that involve relaxing activities that can be done in the evening, preferably before 9:30 pm. They would not prefer suggestions that involve using their phone or watching TV, as these activities have been affecting their sleep quality.

### 第 14 题 · multi-session

旧判定 True；新判定 False；regressed。

**原题：** How many different doctors did I visit?

**本轮回答：** You visited **2 different doctors**: ENT specialist Dr. Patel and dermatologist Dr. Lee.

**参考答案（仅评分阶段使用）：** I visited three different doctors: a primary care physician, an ENT specialist, and a dermatologist.

## 解释边界

这是已暴露的 20 题开发回归，使用 Oracle 历史条件；两轮抽取、生成和评分均有随机性，评分模型与回答模型属于同一配置系列。结果只支持这批题的观察，不能证明架构普遍正确或差异完全来自某一项改动。原始评分不会因人工核查被覆盖。

本地验证新增 22 项通过；基线与分支均有相同 2 项旧断言失败、3 项数据库鉴权前置错误。最终冻结前的局部机制＋来源检查 27 项通过，未声称所有测试通过。

完整性：`{"baseline_files_unchanged": true, "changed_baseline_files": [], "unrelated_payloads_unchanged": true, "source_unchanged": true, "runtime_configuration_unchanged": true, "main_checkout": {"head_unchanged": true, "status_unchanged": true, "index_unchanged": true}, "remaining_owned_collections": []}`。

[逐题结果](question-results.json) · [汇总](evaluation-summary.json) · [冻结记录](freeze.json) · [执行与清理](execution.json)

最终源码 SHA256：`0769a801049683bdf4486654cee3e2311e5c851e92785818f6a5d79ba301de81`。
