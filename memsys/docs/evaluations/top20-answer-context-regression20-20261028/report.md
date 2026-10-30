# Top-20 分支：回答条件与表格兼容复测

分支 `codex/memory-top20-refinement`；提交 `6b1216c2902546e962c9112dcfdf42fbdd842591`。

本轮原始评分 **17/20**，上一轮 Top-20 **18/20**，主干历史记录 **19/20**。交付 20/20，有效评分 20/20。

本轮增加已有主体/属性的呈现、保持时间顺序的偏好标题、同一次回答中的身份/阶段/偏好使用指令，以及分区表格兼容。保留 BGE、候选深度、统一 Top-20、模型和输出预算；没有额外筛选或回答审计调用。

完全相同的20道原题按原顺序，独立重建记忆后真实回答。每题清理独立集合，全部答案固定后统一评分。两组对照均使用既有记录，没有重放回答、按得分挑题、选择性补答或改标签。

## 得分与交付

| 指标 | 主干历史记录 | 上一轮 Top-20 | 本轮 |
|---|---:|---:|---:|
| 原始正确题数 | 19/20 | 18/20 | 17/20 |
| 成功交付 | 20/20 | 20/20 | 20/20 |

相对上一轮的同题变化：`{"unchanged": 19, "regressed": 1}`。

相对上一轮，写入＋回答输入 token +1.54%，输出 token +3.98%；生产调用次数保持不变。金额因缺少定价及缓存计价信息而未知。

| 题型 | 主干历史记录 | 上一轮 Top-20 | 本轮 |
|---|---:|---:|---:|
| single-session-user | 3/3 | 3/3 | 3/3 |
| single-session-assistant | 2/3 | 3/3 | 3/3 |
| single-session-preference | 3/3 | 2/3 | 2/3 |
| knowledge-update | 3/3 | 2/3 | 2/3 |
| multi-session | 4/4 | 4/4 | 3/4 |
| temporal-reasoning | 4/4 | 4/4 | 4/4 |

## 用量与体积

| 指标 | 主干历史记录 | 上一轮 Top-20 | 本轮 |
|---|---:|---:|---:|
| 写入＋回答 LLM 成功调用 | 53.00 | 53.00 | 53.00 |
| 写入＋回答输入 token | 432,194.00 | 360,213.00 | 365,777.00 |
| 写入＋回答输出 token | 297,192.00 | 250,054.00 | 260,001.00 |
| 抽取输入 token | 246,077.00 | 269,771.00 | 266,380.00 |
| 抽取输出 token | 294,342.00 | 246,941.00 | 256,524.00 |
| 回答输入 token | 186,117.00 | 90,442.00 | 99,397.00 |
| 回答输出 token | 2,850.00 | 3,113.00 | 3,477.00 |
| 平均读取＋答题耗时（秒） | 50.24 | 19.37 | 19.17 |
| 写入＋读取阶段耗时（秒） | 4,700.06 | 2,989.80 | 3,501.00 |
| 已存事实条数 | 1,137.00 | 968.00 | 1,046.00 |
| 其中助手事实 | 851.00 | 700.00 | 786.00 |
| 事实正文＋值 UTF-8 字节 | 724,161.00 | 697,409.00 | 707,409.00 |
| 入提示事实总数 | 759.00 | 317.00 | 313.00 |
| 每题平均事实数 | 37.95 | 15.85 | 15.65 |
| 每题最大事实数 | 128.00 | 20.00 | 20.00 |
| 平均记忆正文 UTF-8 字节 | 34,490.95 | 13,865.30 | 14,954.70 |
| BGE 窗口总数 | 716.00 | 716.00 | 693.00 |
| BGE 总耗时（秒） | 235.26 | 272.01 | 258.70 |

本轮用途分项（评分另列，embedding 为本地服务）：

| 用途 | 成功调用 | 输入 token | 输出 token |
|---|---:|---:|---:|
| answer | 20 | 99,397 | 3,477 |
| embed | 203 | 400,946 | 0 |
| embed_query | 20 | 400 | 0 |
| extract | 33 | 266,380 | 256,524 |
| judge | 20 | 3,769 | 841 |

缺少完整定价时货币费用未知；成功请求账本不保证覆盖失败请求用量。评分是评测开销，单列于生产调用之外。

成功请求账本记录传输重试 1 次；逻辑调用观测器不能还原每次 HTTP 尝试。写入客户端未保存逐次失败的原因，不能据重试次数判定网络或上游服务归因。

耗时来自不同时间的真实调用，含服务速度、重试与本地模型冷加载变化，不能单独归因于架构改良。记录数或装入数也不等于语义召回率、命中率或逐条使用率。

## 流程与结构

源会话 33 段，消息 397 条；原文一致 True；未完成会话 0；部分协议会话 0。

写入计数：`{"extract_calls": 33, "extract_model_facts": 642, "extract_rejected_count": 0, "extract_repaired_count": 0, "literal_structures": 407, "literal_structures_before_compaction": 462, "literal_structures_collapsed": 55, "literal_structures_skipped": 0, "covered_literal_structures": 3, "duplicates": 0, "relation_edges": 478}`。

额外选择模型调用 0；BGE 降级 0 题；预算补充扩展 6 题。

输出结束原因：`{"stop": 73}`；观测器记录的最终调用异常 0 次。

装入与调用核对：`{"top20_violations": [], "budget_violations": [], "selector_calls_in_trace": 0, "selector_calls_in_ledger": 0}`。

## 结果核查

原始评分17/20，上一轮Top-20为18/20，主干历史记录19/20；20/20正常交付。酒店建议可见地应用了明确偏好，分区表格兼容也通过真实调用，但岗位前提仍未正确处理，医生就诊题出现新的回退。不能据本轮声称质量改善，当前不宜合入主干。

- 第 14 题：新增原始评分回退。原文用户明确说过“which is why I saw Dr. Patel”，但本轮写入只保留该医生的诊断、处方和计划复诊关系，没有保留这次已发生的就诊动作。三位医生相关事实已入提示；23条用户事实中未选中的三条分别是盐水冲洗频率、补水睡眠、肠镜后饮食，与该就诊动作无关。回答报出三位参与治疗的医生后又说只有一位明确就诊，未准确回答就诊人数。断点涉及抽取丢动作及最终回答的过度保守；不是这次Top-20把该就诊事实截掉。
- 第 10 题：原始判错保持。初始4人与后来5人的事实，以及Senior Software Engineer的岗位信息均已保存并选入提示，本题14条用户事实全部入选。回答这次选用了初始4人，纠正了上一轮采用后来5人的时间阶段错用；但仍直接把这个数量作为没有记录支持的Software Engineer Manager岗位问题的答案，只在后句更正岗位名。缺失前提的处理仍不稳定。
- 第 8 题：自动评分仍为错误。原始用户历史明确包括新餐具收纳架、花岗岩台面划痕、轻微水龙头漏水和垃圾处理器问题；这四项均已保存、召回，实际回答也分别用于清洁和整理建议。这份输出不能直接归类为没有使用个人背景。评分响应未给出具体判错理由，本轮无法确认该项判错是否合理；原始False标签保持，未据人工意见提高总分。
- 第 7 题：原始判定仍通过，本轮可见的个性化比前轮充分。11条用户事实全部入选，其中明确偏好是带屋顶泳池或阳台热水浴缸的酒店。实际回答把推荐的屋顶泳池与用户喜欢特色设施的偏好连接，并提醒核对开放和使用条件。本核查只确认回答对个人偏好的适配表达，不独立验证酒店现实设施或价格。
- 第 4 题：原始判定通过，但答复仍说没有最终决定记录、只是后来使用过该名称。原始用户多次以该名称继续设计；本轮3条已存用户偏好都含该名称且全部入选。至少仍存在回答过度保守的疑点，不能把机器通过当作该问题已完全解决。
- 第 12 题：本轮真实抽取生成了一条包含3个sections的table值，成功入库，完整JSON值与原始模型输出相等，未丢弃分区字段。该题两段历史均完成协议处理，原始评分通过。兼容修复同时有本地测试及真实格式传递证据；不据此声称整份历史所有语义均无遗漏。
- 生产成功响应账本记录1次传输重试，发生在第12题的第二次抽取；该逻辑调用累计耗时约365.15秒后成功。客户端未保留第一次失败的HTTP状态或异常详情，不能进一步区分上游服务与网络原因；失败尝试的用量也未单独返回。没有手动重答或输出触顶重试。

已暴露的20题开发回归，使用历史对照，未新增同期基线或消融；本轮重新抽取，写入内容有变化，无法分离每项修改和随机性的因果贡献。没有逐条核验全部397条消息，也没有完成独立人工重标。以上是可从原文、已存事实和最终文本定位的断点，不是模型内部思考的解释。所有机器标签和原始失败保留。

| 序号 | 题型 | 上轮判定 | 本轮判定 | 变化 |
|---|---|---|---|---|
| 1 | single-session-user | True | True | unchanged |
| 2 | single-session-user | True | True | unchanged |
| 3 | single-session-user | True | True | unchanged |
| 4 | single-session-assistant | True | True | unchanged |
| 5 | single-session-assistant | True | True | unchanged |
| 6 | single-session-assistant | True | True | unchanged |
| 7 | single-session-preference | True | True | unchanged |
| 8 | single-session-preference | False | False | unchanged |
| 9 | single-session-preference | True | True | unchanged |
| 10 | knowledge-update | False | False | unchanged |
| 11 | knowledge-update | True | True | unchanged |
| 12 | knowledge-update | True | True | unchanged |
| 13 | multi-session | True | True | unchanged |
| 14 | multi-session | True | False | regressed |
| 15 | multi-session | True | True | unchanged |
| 16 | multi-session | True | True | unchanged |
| 17 | temporal-reasoning | True | True | unchanged |
| 18 | temporal-reasoning | True | True | unchanged |
| 19 | temporal-reasoning | True | True | unchanged |
| 20 | temporal-reasoning | True | True | unchanged |

## 验证边界

新增 21/21 条本地检查通过；扩展检查 172/198 通过。基线已有 6 项断言失败及 20 项错误，本轮新增失败 0；旧失败未宣称通过。脚本化模型只验证数据传递和调用流程，不验证真实生成正确性。

已暴露的开发回归，不能作为盲测或泛化结论；两轮重新抽取、生成及评分且发生在不同时间。原始标签保留，本轮没有选择性补答、额外模型复核或运行期间的产品代码改动。

主干、部署配置和旧结果均未切换或覆盖。完整性：`{"baseline_files_unchanged": true, "changed_baseline_files": [], "unrelated_payloads_unchanged": true, "source_unchanged": true, "runtime_configuration_unchanged": true, "main_checkout": {"head_unchanged": true, "status_unchanged": true, "index_unchanged": true}, "remaining_owned_collections": []}`。

[逐题结果](question-results.json) · [用量汇总](evaluation-summary.json) · [冻结记录](freeze.json) · [执行与清理](execution.json)

最终源码 SHA256：`d07c8832c94ffa54b790feb3e214bcec8bb83368efcac8b6dbd54a8cd2571afc`。
