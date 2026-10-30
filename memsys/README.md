# memsys：长期交互式 AI 记忆系统

## 项目资料

- [技术方案初稿](docs/proposal-draft-20261029/长期交互式AI记忆系统技术方案初稿.docx)：整体流程、选型理由、开源参考及21轮评测对照，沿用原版流程图。
- [可交互流程图](docs/proposal-draft-20261029/memory-workflow.html)：下载或克隆后在浏览器打开。
- [历史评测报告](docs/evaluations/README.md)：主干、分支和定向复测的原始报告；同题组与不同样本分别标注。
- [学生手册](../DIVE-Student-Handbook.pdf)与[原分析材料](../DIVE-记忆系统分析.docx)；[本机部署说明](docs/local-deployment.md)。

本次代码快照来自 `codex/raw-chunk-rag` 实验分支，包含原文分块路径，默认 `recall_mode` 仍为 `facts`。完整调用记录和本机数据继续保存在本地 `runs/`，密钥通过环境变量或本机配置提供。

2026-10-27 移除回答审计后的固定20题真实复测为 **20/20判分正确、20/20正常交付**，上轮同题为18/20、19/20；判分上2题改善、0回退，拒答题4/4。32段历史、364条消息重新入库，20次答题结束后才启动20次评分；流程失败、输出截断、传输重试均为0。**内容复核仍发现慢炖锅题未使用已召回的腰果酸奶目标，通用化并未解决，评分模型仍将其判对。** 旧记录和既有数据核对通过，运行期间未改产品、配置或评分，未补答或改分。见[完整20题结果与内容边界](runs/memory-regression20-no-answer-audit-20261027/report.md)。这是同题开发回归；基线早于其他修复，不能单独归因于移除审计或证明架构普遍正确。

2026-10-27 已按用户要求删除回答阶段证据审计及其JSON输出协议，网页与公共评测宿主直接生成、交付正文，不再要求证据清单、计算元数据或目标ID。个性化与信息不足时保留不确定性的指令、记忆预算和输出扩容继续保留。70项直接相关检查通过；额外检查18项通过，1项旧SQLite观测器兼容错误在修改前源码同样复现。两个自编真实模型请求均按新流程交付；仅重载记忆API，既有数据与运行配置核验一致。未复跑公开评测，不宣称正确率提高。见[移除与验证记录](runs/remove-answer-audit-20261027/report.md)。下列审计结果属于各次历史版本。

2026-10-27 用户指定重跑上一轮20题中的两道未通过题：**2/2判分正确、2/2正常交付**，上轮同两题0/2。鱼缸题本次答17条，入库阻断未复现；电影节题本次答4个，包含上轮漏计的Austin Film Festival。5段历史、60条消息重新入库，各回答一次后统一评分；原历史、原题、配置和评分规则保持一致，未改产品、重载服务、补答或改分。两题审计均有未完全绑定项，未拦截或改写正文；本次只复测两题，不能换算整组20题成绩，也不能证明缺陷已稳定消除。 临时集合、1395个既有记录文件和既有集合payload核验通过；见[原始结果与配对记录](runs/memory-regression2-task-context-20261027/report.md)。

2026-10-27 已部署抽取逐条隔离与建议任务组织修复：完整数组内的坏条目单独留档，有效事实继续发布并保留信息缺口；同次回答先选适用目标和限制，再生成具体建议，审计异常不阻断正文。验收中发现的协议JSON外泄也已修复，原始失败全部保留。最终201项相关检查通过（2项修复前已复现的旧断言明确排除），8个预写自编场景各新建记忆库运行两次，16次严格交付与内容复核符合预设要求，其中4次为受控故障注入。这是自编功能验证；未复跑原20题，不能换算整体准确率。 既有数据与配置未变，未重抽私人历史；见[修复、原始失败及部署报告](runs/task-context-isolation-repair-20261027/report.md)。

2026-10-27 当前部署版本完整重跑上一轮固定20题：**18/20判分正确、19/20正常交付**；上轮为18/20、20/20，逐题2题改善、2题回退，跨会话从4/4降至2/4。新增回退包括抽取结构校验导致一段历史未发布、整题无法回答，以及电影节事实已进入实际提示但漏计一项。衣着属性本次完整保留并答出；慢炖锅题虽被评分模型判对，已召回的腰果酸奶目标仍未用于建议，通用化实际未解决。**本轮架构流程验收未通过。** 32段历史、364条消息逐字归档，352条完成记忆处理；未补答、改分或改产品，临时集合及850个既有记录文件、既有数据核验通过。详见[完整20题复测与诊断](runs/memory-regression20-fidelity-20261026/report.md)。这是已暴露题集的开发回归，不能据此证明架构普遍正确。

2026-10-26 用户指定复测最近20题中的两道错题：**2/2判分正确、2/2正常交付**，上轮同两题为0/2。两段历史、24条消息重新入库，每题回答一次，全部回答后独立评分；原始阶段输入、运行配置和评分程序保持一致，本轮没有修改产品或补答。衣着属性本次保留并答出，腰果酸奶目标本次独立保留且用于回答；但慢炖锅正文仍有较多通用建议，不能宣布通用化已彻底消除。临时集合、旧记录哈希和既有数据核验通过。详见[两道错题复测](runs/memory-regression2-fidelity-20261026/report.md)。这是定向开发回归，没有重跑完整20题，不能换算当前整组分数。

2026-10-26 已部署原子事实与用户上下文传递修补：同次抽取明确保留独立属性、目标、限制及请求语境；建议类用户事实优先顺序延续到最终展示，首次回答使用相应任务适配要求。172项相关机制检查通过；四个预写独立场景各重新入库两次，八次均交付，逐条复核符合预设功能要求。首轮一次多余的来源不确定性及所有原始结果保留。临时集合清理、本机重载、既有数据及配置核验通过，未自动重抽私人历史。详见[修补报告](runs/memory-fidelity-repair-20261026/report.md)。这是功能验证，本次未复跑公开评测，不能据此声称整体准确率提高。

2026-10-26 当前仅诊断版本完成20题同题开发回归：**18/20正确（90%）、20/20交付、流程失败0、拒答4/4**。同20题在引入硬校验前为18/20，硬校验版本为14/20且有6题流程失败；本次相对原正常版本2题改善、2题退步，总分持平。助手信息和偏好各错1题，其余四类全对。8题字段审计不完整却全部判对，两道错题反而通过字段审计，程序均未改写正文。32段历史、364条消息重新入库；本轮产品源码和配置未改，旧记录2218个文件哈希、临时集合清理与既有数据核验通过。详见[20题同题回归](runs/memory-regression20-audit-only-20261026/report.md)。结果支持流程恢复及当前方向的有限有效性，仍存在逐题回退；样本已暴露且源码、预算等与历史版本存在差异，不能作为泛化或单项改动的因果证明。

2026-10-26 移除证据硬规则后，用户指定的三道既有拦截题真实复跑为 **3/3交付、3/3判分正确**，上一轮同三题为0/3。五段历史、56条消息从头写入，原题、模型、预算和评分程序保持一致；三题仍有审计告警，均未改写正文。仅执行三次答题调用，未按结果补答；临时集合清理、旧评测文件哈希及原有数据核验通过。最后一题抽取经历两次配置内网络重试后成功。详见[三题开发回归](runs/memory-regression3-audit-only-20261026/report.md)。这是已暴露样本的回归，不能单独证明规则变更的因果效果或全量准确率。

2026-10-26 已按用户要求移除证据硬规则对正文的拦截、替换与纠正：证据和计算检查仅作诊断，审计异常也不能阻止完整答案交付。模型直接生成完整正文和计算结果，程序不再填充计算占位符或因对象标识缺失把整篇替换为 unknown。76项相关检查通过，本机API已重载，数据及运行配置指纹一致。变更与部署记录见[诊断模式验证](runs/evidence-audit-only-20261026/validation.json)和[本机重载记录](runs/evidence-audit-only-20261026/deployment.json)。实际依据不足时仍要求回答保留不确定性。

2026-10-26 此前指定七道流程失败题的真实复跑为 **7/7交付、3/7判分正确、5/7证据绑定审计不完整**。其中两题被当时的计算规则覆盖为 unknown；这组结果属于上述硬规则移除前的版本。完整记录见[七题开发回归](runs/memory-regression7-system-flow-20261026/report.md)。

2026-10-26 已部署本轮流程修复：审计异常独立记录；计算操作数直接核对来源、单位与对象身份；输出触顶时有界扩大预算；长事实完整分段嵌入。最终代码134项相关检查通过，真实模型场景 **8/8交付、7/8内容通过、6/8审计完整通过**。剩余计算题明确返回无法核实，内容检查仍记为失败。144条自编记录的名称／数值覆盖及真实输出扩容检查通过。原有数据和历史评测文件指纹一致。详见[本轮验收记录](runs/system-flow-repair-20261026/acceptance.json)及[本机重载记录](runs/system-flow-repair-20261026/deployment.json)；本轮没有重跑公开评测。

2026-10-26 上轮“内容与证据契约修复完成”的判断已撤回：最后一轮真实模型功能测试原始结果为 **5/8 严格通过**；旧响应的适配器回放不能证明新生成稳定。随后30题回归出现流程失败与输出截断。原始响应、失败和评分保留在[契约修复记录](runs/memory-contracts-repair-20261025/summary.json)与[30题回归记录](runs/memory-regression30-contracts-20261026/failure-breakdown.json)。本轮先修审计阻断、预算和截断恢复，验收分别报告答案交付、内容正确性及审计完整性。

2026-10-25 按用户要求另选30题作对照，结果为 **25/30（83.3%）**；沿用上一组37/40（92.5%）的源码、模型、配置和评分程序。六类各5题，题号及49个原始历史会话与上一组均不重合。30题全部正常回答，写入、答题及判分流程异常均为0，拒答4/4；每题只回答一次，统一评分，冻结输入与临时集合清理检查通过。详情见[另组30题对照报告](runs/memory-control30-semantic-20261025/report.md)。两组属于不同样本，不能把分数差异直接解释成版本效果；本组来自有历史暴露的公开题池，不能作为新盲测结论。本轮未修改产品。

2026-10-25 语义边界修复后，用户授权同40题开发回归为 **37/40（92.5%）**，对照上轮32/40（80%），提高12.5个百分点；逐题5题改善、0题退步。40题均正常回答，69个历史会话写入失败0，拒答4/4。四份输入逐字节相同，模型、预算及评分程序相同；每题只回答一次，全部回答后统一评分，临时集合清理核验通过。评分有1题首次标签未完成，按既定协议追加1次裁判调用，未重答或人工改分。详情见[同题重跑报告](runs/memory-regression40-semantic-20261025/report.md)。这是已暴露样本的开发回归，不能作为盲测泛化或单项修复的因果证明。

2026-10-25 已完成语义边界修复并重载本机 API：加强助手具体内容保留、状态与人物角色区分、跨事实时间证据使用；明确标签下的短数值字段有字面补充，空抽取有独立覆盖提示。228项相关模块检查、8项真实Qdrant检查和9个自编真实模型场景的10次请求通过，既有数据指纹一致。保留具名对象、整批单次抽取及facts模式，没有自动补抽私人历史。边界与功能验证见[修复报告](runs/semantic-boundaries-repair-20261025/report.md)；37/40是随后获用户授权执行的同题开发回归。

2026-10-25 对象式抽取恢复后的同40题开发回归为 **32/40（80%）**，上轮15/40（37.5%），增加42.5个百分点。40题均正常回答，69个历史会话写入失败为0；逐题17题改善、0题退步，拒答4/4。四份冻结输入逐字节一致，全程未补答，临时集合全部清理。详情见[同题重跑报告](runs/memory-regression40-objects-20261025/report.md)。这是已暴露样本的开发回归，不是新盲测。

2026-10-25 已撤回列式抽取并重载本机 API，恢复修改前的命名字段 JSON 对象。当前抽取专用配置为32,768输出token、360秒超时，部署升级保留该预算。207项当前链路检查、8项真实Qdrant检查和3个自编真实模型场景通过；48条自编事件的完整输出为12,428 token。原有数据记录指纹一致。详情见[对象式抽取恢复报告](runs/object-extraction-repair-20261025/report.md)。

撤回前的40题开发回归原始错误、回答和评分保持在[原报告](runs/memory-regression40-20261025/report.md)，未被本次重跑覆盖。覆盖补取的字节／条数边界修复、超大库有界召回和同请求内相同重排输入复用继续保留，早先实现与性能边界见[修复记录](runs/reliability-repair-20261025/report.md)。

当前采用本机原生部署，打开 [MemSys 聊天页面](http://127.0.0.1:8848) 即可使用，记忆 API 与页面共用这个地址。Qdrant、Embedding 与重排均运行在本机；回答及抽取 LLM 沿用现有 OpenCode API。启动、停止、令牌位置及 Agent 接入方式见 [本机部署说明](docs/local-deployment.md)。

可接入不同 Agent 的记忆服务：原文、事实、来源与任务统一保存在 Qdrant。主流程为检索相关旧记忆 → 单次 LLM 抽取 → 完整事实字段去重与新增；默认检索不调用生成式 LLM。最终答案由宿主 Agent 生成。本机聊天宿主核对字面引用与支持的计算，不增加回答模型复查；外部 Agent 自行负责答案。

独立的新旧事实裁决及自动取代已移除。内容变化保存为新记忆，旧内容继续保留，宿主结合事实的记录时间、事件时间和状态使用。旧库已有版本链仍可查，不改写历史数据。每个非空写入批次整批抽取；不按长度自动分块或截断消息。输出触顶时对相同完整输入增加输出预算，最多额外两次；达到上限仍不完整时整批失败并保留原文，不发布残缺事实。

默认 `memory.recall_mode="facts"`：长期记忆只检索、重排和返回抽取后的事实，不自动扩展来源原文，不用原文兜底缺失事实；列举与分页也只覆盖事实。原文和来源编号仍保存在 Qdrant，可通过 `/memsys/sources` 明确查阅。`facts_and_sources` 保留为显式诊断模式，该模式的准确率必须单独标注。

V7 新增工作记忆与实体关系索引。`chat_window_turns` 现在表示最近完整对话轮上限，另受 `chat_window_tokens` 控制；当前未完成的用户输入单独受总输入预算约束。旧轮次退出窗口前须成功整理为记忆，失败时保留消息并明确报错。一次抽取同时返回实体与来源支持的关系；别名需要可验证的显式命名声明。同名实体的明确不同身份和不同事件发生保留独立标识。

`entity` / `relation` 索引与事实统一存入 Qdrant，关系只引用事实 ID，不进入向量／词法候选集成为第二份证据。读取采用语义检索、实体定位及最多两跳的有界扩展，按事实 ID 去重；遗忘事实不能成为关系桥梁，用户节点不作为全库扩展入口。宿主首次生成前可以在剩余输入预算内补入遗漏事实；仍无法覆盖时不得声称完整清单或最终总量。抽取遗漏本身不能靠图索引恢复。旧事实可仅用已有字段重建弱关联索引，完整关系由之后的新写入形成。构建和唯一一轮32题收尾计划见 [V7 计划](docs/working-relational-memory-v7.md)。

事实正文和 `value` 使用统一格式；正文已经包含的字面值不重复附加。列表、表格和动作序列使用紧凑 JSON 保留顺序、实体、数值和条件。字段不再静默截为固定字符数；预算不足时整条事实退出并记录遗漏。相同摘要下不同的值、条件或事件分别保存，完整字段相同时才关联来源去重。结构覆盖提示不触发额外 LLM 调用；仅输出触顶可以按配置增大预算重试。

同一次写入还用确定性解析器保留助手 Markdown 表格、编号列表和明确标签下的短数值字段：表格保存完整单元格；列表保存名称、原编号、短详情及一层显式子项，并保留标题上下文；数值字段保留字面标签、值和单位。超出解析边界的内容仍由整批模型抽取处理，不截短为替代值。来源片段只用于溯源，不进入事实上下文。该步骤不增加抽取 LLM 调用，也不读取评测问题或答案。普通段落、代码围栏及格式不明确的结构不由该解析器推断。写入报告区分模型事实数、字面补充数与逐轮角色覆盖声明；协议和字面核对成功不等于语义覆盖已经验证。

V6 会检查同轮模型事实是否精确保留了列表位置、动作或表格单元格；确认覆盖时保留较丰富的模型事实并关联字面来源，不再新增重复回补。读取时，相同结构值且解释元数据一致的事实共用一份值展示，各自描述、编号和条件仍保留；HTTP条目仍返回完整值。建议操作优先选择已召回的用户相关事实，其他操作不采用这项优先策略。

事件状态保留为 `planned`、`ongoing`、`completed`、`cancelled` 或 `unknown`，兼容模型明确输出在语义字段或事件值中的状态；不从置信度或自然语言猜测。事件日期、记录日期与相对时间分别展示。没有日期或发生标识的事件，不仅凭相同描述跨会话合并。已有私人历史不重新抽取或自动补状态。实施与自编功能验证见 [V6计划](docs/fact-memory-improvement-v6.md)。

事实值现在也参与稠密/词法索引和重排。升级前的索引需运行 `memsys reindex --user <用户>`；重建只读取已存事实和原文并生成向量，不重新抽取，也不能恢复旧值已经被截断的内容。旧数据可以从安全历史显式回补可解析的结构事实；其余丢失细节需在新空间从原历史重新构建。修复回归及验证边界见 [事实保真报告](runs/fact-retention-20261013/report.md)，范围为原24题和预选额外24题，没有可选消融或500题验收。

- 在线存储：Qdrant 服务器，不使用 SQLite。旧 SQLite 只通过显式只读迁移命令导入。
- 检索：事实内容的服务端稠密向量 + 稀疏词法检索、融合、重排和预算组装；原文来源展开需显式诊断模式。
- 接入：HTTP、域名客户端、MCP、hook、宿主模型代理及 Python API。
- 部署：`deploy/local.py` 管理本机原生服务；旧云端部署模板与上传包已移除。
- 检查：关键存储和接口功能测试，以及用户明确授权的小样本修复回归；不自动扩展评测范围。

当前流程与状态见 [架构说明](docs/architecture.md)。历史评测脚本及 SQLite 测试保留供参考，尚未适配这次主存储变更，不作为当前架构的验证依据。

## 本机聊天页面

浏览器打开 `http://127.0.0.1:8848`，本机部署自动连接当前账户，无需向浏览器填写模型密钥。支持新建与继续对话、历史搜索、重命名、归档、Markdown/代码展示、复制回复和记忆参考。页面采用简洁的浅色界面。对话保存到 Qdrant，刷新页面后仍可继续。

网页是独立的宿主 Agent：检索事实记忆后调用一次回答模型，不增加答案复查。新建对话或切换到侧栏另一段已有对话时，离开的会话进入后台抽取流程；长对话沿用已有整理阈值。普通发布冲突在短事务内重试，复用已有抽取结果和向量。默认不会将其他会话尚未整理的原文补进回答上下文；后台尚未发布的内容需等待整理完成才能成为长期记忆。当前对话仍保留受输入预算限制的最近 12 条消息窗口。每次回答都会自动检索相关记忆，没有关闭选项；检索失败时明确返回错误，不跳过记忆继续生成。归档将对话移出列表，已有记忆保留。

等待期间显示检索/回答状态，完成后显示完整答复；尚未接入模型逐 token 流式输出。发送失败可以显式重试，已完成的同一消息重试不会再次调用模型。网页功能通过合成模型检查；已完成真实模型的跨会话验证及LongMemEval小样本评测，具体配置、源码、输入和结果以各次冻结运行记录为准。

## HTTP 契约

所有记忆端点使用 `X-Memsys-Token`；REST 也接受 Bearer 令牌。用户令牌只能操作绑定的 `user`。公网监听时没有配置鉴权会拒绝启动。

| 端点 | 内容 |
|---|---|
| `POST /memsys/sessions` | 打开或复用外部会话编号，返回 `session_id` |
| `POST /memsys/turns` | 按 `session_id` 记录带角色的原文；每条支持 `idempotency_key` |
| `POST /memsys/end` | 封闭会话追加并排队后台整理 |
| `POST /memsys/retry` | 修正失败原因后，按 `job_key` 显式重试自己的失败任务 |
| `POST /memsys/recall` | 返回记忆、规则、来源、状态、版本关系、覆盖信息 |
| `POST /memsys/recall-pages` | 返回有界证据分页及完整性说明 |
| `POST /memsys/sources` | 按 `E...` 编号取得未屏蔽的原文与角色 |
| `POST /memsys/history` | 按 `F...` 编号取得版本链，已遗忘正文隐藏 |
| `POST /memsys/forget` | 主动遗忘并屏蔽受影响的来源和近期对话 |
| `GET /memsys/status` | 当前会话、任务状态及失败原因；用户令牌只返回自己的信息 |

普通 Top-K 检索不保证完整枚举。做完整列表或总量前，Agent 应查看分页与覆盖信息，自己判断相关性与结论。

## 旧库迁移

停止旧写入端后，显式导入到空的 Qdrant 用户/命名空间：

```bash
memsys migrate-sqlite --source /绝对路径/旧库.sqlite --user alice --namespace imported --config config.json
```

只读源文件，不调用模型，不重算向量；原文、安全视图、来源和版本保留。旧索引快照不导入。旧文件不会被自动删除。迁移只在你选择源库后执行，本轮没有迁移真实用户数据。

维护命令现在使用 `--user` 与存储配置，不再使用 `--db`。物理清理要求先停掉全部读写客户端，再显式执行带备份的 `cleanup --apply --offline`。普通归档/主动遗忘不需要停机。

## 环境

- Python 3.9 及以上（推荐 3.11+）和 Qdrant 1.16+ 服务器。核心 REST 客户端只用标准库；部署模板固定 Qdrant 1.19.2。
- 模型走 OpenAI 兼容接口：DeepSeek、OpenAI、通义千问等在线服务，或本地 [Ollama](https://ollama.com) / vLLM 都可以；Anthropic 接口需要另装官方 SDK（`pip install anthropic`）。
- 中英文混用时 embedding 推荐多语言模型，例如 Ollama 的 `bge-m3`。
- 模型都可以不配，见下面的"配置模型"。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[rerank,fast]'  # 安装本地 BGE 重排与向量加速，之后可以直接用 memsys 命令
```

## 本地接入说明

`--fake` 仅替换模型，存储仍需要可用的 Qdrant 服务器。它不表示使用本地 SQLite。当前不启动评测或性能测试。

```bash
memsys chat --fake --user demo --clock sim --at "2026-10-01 09:00"
```

## 配置模型

记忆系统不自带模型，也不要求一定有模型。每个角色按这个顺序找，找到就用：

1. 配置文件（`--config`、环境变量 `MEMSYS_CONFIG`、当前目录的 `config.json`、`~/.memsys/config.json`）里写明的
2. 宿主借给的：代理借上游模型和 agent 带来的 key，MCP 借客户端的模型，Python 调用方传进来的函数
3. 环境变量：`MEMSYS_LLM_BASE_URL` + `MEMSYS_LLM_MODEL`（+ `MEMSYS_LLM_API_KEY`），或者 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`SILICONFLOW_API_KEY`、`ANTHROPIC_API_KEY`；embedding 同理用 `MEMSYS_EMBED_*`
4. 本机 Ollama（`OLLAMA_HOST`，默认 `localhost:11434`）
5. 都没有：降级运行

| 档位 | 有什么 | 能做什么 |
|---|---|---|
| L0 | 什么模型都没有 | 原文入库、时间线、遗忘；默认无抽取事实可召回（之后用 `memsys rebuild` 补）；原文关键词检索需显式诊断模式 |
| L1 | 有抽取模型，没有语义 embedding | 抽取、去重与新增记忆；向量只有词面相似度 |
| L2 | 抽取模型 + 语义 embedding | 抽取、去重与新增；语义和关键词检索及可选 BGE 重排 |

```bash
memsys doctor            # 现在是哪一档、每个角色用的哪个模型、从哪找到的（--check 会真的调用一次）
memsys init              # 按当前环境写一份 config.json
```

通过配置固定模型和存储地址：

```bash
cp config.example.json config.json
export DEEPSEEK_API_KEY=...        # key 只放环境变量，配置里只写变量名
```

`roles` 决定谁做什么：`memory` 负责单次记忆抽取，`embedding` 负责向量；`extract` 可以覆盖抽取模型。最终回答由宿主 Agent 生成，示例适配器才使用 `chat`。默认检索关闭查询规划模型，重排使用 BGE；`query` 与 `rerank_backend=llm` 为显式启用的可选功能。旧 `roles.resolve` 告警后忽略，不加载裁决模型。全部参数见 `src/memsys/config.py` 的 `MemorySettings`。

在 `pricing` 里按服务商价目表填单价（每百万 token），并记录币种、价格日期和官方来源。例如
`{"input": 1.0, "output": 2.0, "cached_input": 0.5, "currency": "USD", "as_of": "2026-10-09", "source": "官方价格页 URL"}`；这些数字仅演示字段，不是服务商报价。
`null` 表示价格尚未配置，本地模型显式填 `"input": 0, "output": 0`。报告把未知费用与零费用分开，
不同币种分别汇总；每次调用的价格快照随账本保存，之后修改配置不会改写旧费用。

换了 embedding 模型后用 `memsys reindex --user <用户>` 重算向量；降级期间存下的原文，有了记忆模型后用 `memsys rebuild --user <用户>` 补抽事实。

### OpenCode Go 与额度自动切换

在 `.env` 中设置 `OPENCODE_API_KEY`。`config.example.json` 包含检测到的全部 38 个 OpenCode Go 模型 ID，
`chat`、`memory`、`judge` 默认使用 `opencode-gpt-6-luna` 的统一候选池：GPT 6 Luna → GPT 5.6 Luna →
Claude Haiku 5.5 → Grok 4.7 / 4.6 → Muse Spark，再到 DeepSeek、GLM、Kimi、Qwen、MiniMax、MiMo、Hy、
LongCat 和 Step；实验与未知来源型号放在后面。完整顺序在 `fallbacks` 中，可以直接调整。
HTTP 接口使用 `deepseek-v4.1-flash` 这样的模型 ID，不加 OpenCode 客户端配置用的 `opencode-go/` 前缀。
模型与协议以 [OpenCode Go 官方接口列表](https://docs.opencode.ai/docs/go/#endpoints) 和
`https://opencode.ai/zen/go/v1/models` 为准。

历史模型实验记录见 [迭代记录](docs/accuracy-campaign-v42.md)，不作为当前默认角色或选型依据。当前有效模型以使用的配置文件为准。

`fallbacks` 用配置中的模型名指定候选顺序，例如：

```json
"fallbacks": {
  "opencode-gpt-6-luna": ["opencode-gpt-5.6-luna", "opencode-claude-haiku-5-5", "opencode-deepseek-flash"]
}
```

候选需要先在 `models` 中定义。额度不足（HTTP 402、明确的 quota/usage-limit 错误）或 HTTP 429 会切换，
同一个配置模型的回答和记忆角色共用切换状态。失败模型冷却 5 分钟，期间跳过；冷却结束后重试更高优先级，
让恢复额度的海外模型重新优先。明确停用、模型不存在或模型专属权限未开启也会跳过；所有候选都不可用时明确报错。
401、普通 403、参数错误和服务故障保留原来的报错或重试行为。Muse Spark 的训练数据权限目前会被工作区设置拦截；
配置保留这两个型号，调用时按服务商返回的权限错误跳过。
调用日志按真正完成请求的模型记 token 和费用，评测来源记录完整候选列表；未知价格仍标为未知。
OpenCode 请求自动带 `memsys/0.1.0` 的 User-Agent 和稳定的会话头，模型切换沿用同一会话标识。
embedding 保持原配置，避免混用不同向量空间。

三种协议都能接入：`api: "openai"` 使用 `/chat/completions`，`api: "responses"` 使用 `/responses`，
`api: "anthropic"` 在 OpenCode 使用 `/messages` 与 `x-api-key`。OpenCode 这三种适配器都只用标准库，
其他 Anthropic 上游仍使用原有的可选官方 SDK。
GPT 使用非思考模式；必须思考或没有统一关闭参数的型号设置 `output_token_headroom`，给最终答案留出输出预算。
Responses 的 JSON 模式使用 `text.format`，Messages 的结构化输出由记忆提示词约束，并走同一个 JSON 解析与修复流程。
Responses 返回消息带阶段时只读取 `final_answer`；回放助手消息保留 `phase`，避免把中间草稿拼进事实或答案。没有阶段字段的服务继续兼容。
未完成响应区分输出长度、服务过滤和其他原因；过滤或拒绝不触发格式改写、拆分重提或换模型。部分JSON不能作为抽取、检索计划或分页证据，失败抽取保留待补队列。
回答层检查输出是否完成；空白或截断输出允许一次简短重试，仍未完成时记为调用失败。
查询规划或重排的非账户服务错误会退回本地检索，降级原因保存在trace；账户错误和最终回答错误仍明确报出。

逐个检查所有已配置型号（不会把候选切换误当作原型号成功）：

```bash
python3 scripts/check_opencode_models.py --workers 3
```

结果写入 `runs/opencode-all-models-check.json`，区分成功、额度不足、访问未开放和其他错误。

### 本地免费的 embedding（Ollama）

没有语义 embedding 时向量只看字面重合（L1），建议在本机用 Ollama 跑一个开源 embedding 模型，不花钱、对话不出本机。

1. 装 Ollama，三选一：`brew install ollama`；官网下载桌面版；或者从 [GitHub 发布页](https://github.com/ollama/ollama/releases) 下载 `ollama-darwin.tgz` 解压到任意目录（不需要管理员权限）。
2. 启动并拉模型：`ollama serve`（桌面版会自己启动），然后 `ollama pull bge-m3`。
3. `config.json` 里把 `roles.embedding` 改成 `bge-m3`（`config.example.json` 已经写好这个模型），`memsys doctor` 应显示 L2。

| 模型（Ollama 名） | 大小 | 许可 | 说明 |
|---|---|---|---|
| `bge-m3` | 1.2 GB | MIT | 默认推荐：中英文都好，1024 维，能处理 8k token 长文本 |
| `qwen3-embedding:0.6b` | 0.6 GB | Apache-2.0 | 更新、更小的多语言模型，可以作为对照 |
| `snowflake-arctic-embed2` | 1.2 GB | Apache-2.0 | 多语言 |
| `embeddinggemma` | 0.6 GB | Gemma 使用条款 | 小，多语言 |
| `nomic-embed-text` | 0.3 GB | Apache-2.0 | 最小，以英文为主，不适合中文对话 |

换模型后向量不能混用：评测会自动只重算向量；对话入口的记忆库运行 `memsys reindex`。

## 对话

```bash
memsys chat --user alice                                     # 真实时钟
memsys chat --user demo --clock sim --at "2026-10-01 09:00"  # 模拟时钟，可以用 /time 推进
memsys chat --user demo --clock sim --at "2026-10-01 09:00" --script docs/demo.txt   # 答辩演示脚本
```

| 命令 | 作用 |
|---|---|
| `/end`、`/new` | 结束会话（触发写入并打印写入报告）；结束后下一句开启新会话 |
| `/time +3w`、`/time 2026-12-01` | 推进模拟时钟，会先结束当前会话，再按新时间跑一次遗忘策略 |
| `/why` | 上一个回答的检索轨迹：查询计划、候选及分数、选入的记忆、上下文与历史的 token 比、模型笔记 |
| `/mem [关键词]` | 列出当前有效的事实，或检索记忆 |
| `/hist F12`、`/hist 关键词` | 某条事实的版本链 |
| `/forget F12`、`/forget E5` | 遗忘一条记忆；事实的来源原文退出检索，其他有效原子事实保留；近期对话也屏蔽受影响的已整理前缀 |
| `/stats` | 记忆规模、各状态数量、保留分最低的条目、本次运行的模型用量 |
| `/model 名字` | 切换回答用的模型，记忆数据不受影响 |
| `/confirm [编号 yes\|no]` | 兼容旧库的待确认记录；新增流程不再生成冲突 |
| `/quit` | 结束会话并退出 |

记忆存在配置指定的 Qdrant 命名空间，`.memsys/traces/` 只保存轨迹。真实时钟下空闲超过 30 分钟，下一句话前会先结束上一个会话（`--idle-minutes`，0 为关闭）。程序意外退出、或退出时模型调用失败，原文都已在日志里，下次启动会补整理没关闭的会话，并重试已关闭会话的持久补抽队列。当前抽取按整批消息进行，不按长度自动分块。HTTP 后台写入的超限、拒绝、输出截断或格式错误会保留为 failed；检查 `/memsys/status` 的任务原因，修正后调用 `/memsys/retry` 显式重试，不会自动拆小重抽。

主动遗忘不会物理删除审计日志。共享原文含有已遗忘事实时标为 `withheld`，退出检索、重排、补抽和索引重建；同轮其他有效原子事实仍可使用，但共享原文里的细节不再提供。受影响会话已经整理的对话前缀也退出回答提示词，新发言仍可使用。默认活跃容量为 5000 条事实、2000 轮原文，超出按保留分归档；显式配置 `null` 可取消上限。聚合、历史和时间范围查询同时搜索归档层，查询向量只计算一次；物理数据库仍随审计日志和归档增长。

## 接入别的 agent

记忆放在 `~/.memsys/`（`--data-dir` 或环境变量 `MEMSYS_HOME` 可以改），同一个用户在不同 agent 里共用。用户身份用 `--user`，代理里也可以每个请求单独指定。

### 代理：agent 一行代码不用改

```bash
memsys serve --upstream https://api.deepseek.com/v1        # OpenAI 兼容的上游
memsys serve --upstream https://api.anthropic.com          # Anthropic 上游
memsys serve --upstream http://localhost:11434/v1          # 本地 Ollama
```

然后把 agent 的接口地址指向代理：OpenAI 接口的 agent 把 `base_url` 改成 `http://127.0.0.1:8848/v1`，Anthropic 接口的改成 `http://127.0.0.1:8848`。key 照常由 agent 发，代理原样转给上游。

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8848/v1", api_key=os.environ["DEEPSEEK_API_KEY"])
client.chat.completions.create(model="deepseek-chat", messages=[...], user="alice")   # user 决定记忆归谁
```

- 每轮取最后一条用户消息去检索，记忆块插在这条消息前面；回答原样转回（支持流式）。调工具的中间步骤不记，工具循环里同一轮只检索一次。
- 用户身份依次看请求头 `X-Memsys-User`、OpenAI 请求体的 `user`、Anthropic 请求体的 `metadata.user_id`，都没有就用 `--user`。请求头 `X-Memsys-Memory: off` 让单个请求不经过记忆。
- 记忆模型默认借上游：同一个上游、agent 带来的 key（只在内存里）、agent 用的模型；`--memory-model` 换成同一上游里便宜的模型，`--no-borrow` 不借。
- 空闲 30 分钟（`--idle-minutes`）或 `POST /memsys/end` 算会话结束，后台整理。Ctrl-C 退出时会先把没结束的会话整理完。
- 记忆出任何错都不影响转发，这一轮不带记忆照常回答。只监听本机地址。

同一个端口还有不经过模型的记忆接口，任何语言都能调：

```bash
curl -s localhost:8848/memsys/turns  -d '{"user":"alice","turns":[{"role":"user","content":"我下个月搬去成都"},{"role":"assistant","content":"好的"}]}'
curl -s localhost:8848/memsys/end    -d '{"user":"alice"}'
curl -s localhost:8848/memsys/recall -d '{"user":"alice","query":"我要搬去哪？","budget":1500}'   # 返回 context，整段放进你的系统提示词
curl -s localhost:8848/memsys/status
```

不给 `--upstream` 时只提供这些接口。

### Claude Code：hook + MCP

hook 由 Claude Code 每轮自动调用：记下用户这句话、注入检索到的记忆、记下最终回答、会话结束时整理。写进 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command", "command": "memsys hook --user me", "timeout": 120}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "memsys hook --user me", "timeout": 60}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "memsys hook --user me"}]}],
    "SessionEnd":       [{"hooks": [{"type": "command", "command": "memsys hook --user me", "timeout": 120}]}]
  }
}
```

再加上 MCP，让模型能主动查记忆、遗忘、看版本链：

```bash
claude mcp add memsys -- memsys mcp --user me
```

hook 是单独的进程，用不了 Claude Code 的模型，记忆模型按"配置 → 环境变量 → Ollama"找（key 可以放在 `~/.memsys/.env`）；什么都没有就是 L0，之后补抽。Claude Code 不建议走代理：它自己发的辅助请求也会被当成对话记下。

### 其他 MCP 客户端

Claude Desktop、Cursor 等在 MCP 配置里加：

```json
{"mcpServers": {"memsys": {"command": "memsys", "args": ["mcp", "--user", "me"]}}}
```

客户端支持 sampling 时，记忆抽取可由客户端自己的模型代跑；不想这样就加 `--no-sampling`。工具：`memory_recall`、`memory_save`、`memory_end_session`、`memory_forget`、`memory_history`、`memory_status`。

### Python 库

```python
from datetime import datetime
from memsys.core import MemorySystem

mem = MemorySystem.open("~/.memsys/myagent", llm=my_llm, embed=my_embed, embedding_name="bge-m3")
# my_llm(messages) -> str；my_embed(texts) -> list[list[float]]。两个都可以不传，不传就自动找或降级

s = mem.open_session("alice", datetime.now())
mem.add_turn(s, "user", "我下个月搬去成都", datetime.now())
mem.add_turn(s, "assistant", "好的，祝顺利", datetime.now())
mem.close_session(s, datetime.now())                 # 整理进长期记忆

rec = mem.recall("alice", "我要搬去哪？", datetime.now())
system_prompt = my_system_prompt + "\n\n" + rec.context   # rec.items 是选中的条目，rec.trace 是检索轨迹
```

## 历史评测工具（本轮不使用）

以下为旧实验记录和旧脚本用法，尚未适配 Qdrant 主存储；不代表本次验证结果，不作为选型依据。

数据从 Hugging Face 的 [xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) 下载 `longmemeval_oracle.json`，放到 `data/` 下。

```bash
# 1. 回放跑分：输出 runs/oracle-v1/hypotheses.jsonl（每行 question_id + hypothesis）
memsys bench run --data data/longmemeval_oracle.json --out runs/oracle-v1 --workers 4

# 先小规模试跑
memsys bench run --data data/longmemeval_oracle.json --out runs/try --limit 20

# 2a. 官方评分（正式成绩口径）：在 LongMemEval 仓库的 src/evaluation 下
python3 evaluate_qa.py gpt-4o /path/to/runs/oracle-v1/hypotheses.jsonl /path/to/longmemeval_oracle.json

# 2b. 本地评分（调参用；提示词与官方逐字一致，可以换更便宜的评分模型）
memsys bench judge --data data/longmemeval_oracle.json --hyp runs/oracle-v1/hypotheses.jsonl

# 3. 评估报告：准确率（总体 / 分题型 / 拒答 / Task-averaged）、上下文与历史 token 比、耗时、token 与费用、错题
memsys bench report --data data/longmemeval_oracle.json --run runs/oracle-v1
```

- **可续跑**：已有答案的题会跳过；单题出错记在 `errors.jsonl`，修好后用同样的命令再跑一次即可。进程在任何时刻被杀都能续跑（写了一半的行会被跳过，写到一半的记忆库会重建）。
- **配置一致**：续跑时模型参数、源码、记忆参数、提示词或数据文件和已有答案不一致会拒绝，避免一份答案混着两套配置；确实要接着跑加 `--allow-config-change`（报告会标出）。`run_config.json` 保存 Git commit、源码和提示词哈希、实际模型参数与价格。旧运行缺少这些来源记录时，使用新输出目录。
- **写入缓存**：每题的记忆库在 `runs/<名字>/stores/`。只改读取参数或回答模型时会直接复用；写入参数或记忆模型变了会自动重建；只换了 embedding 时只重算向量（更新裁决当时按旧向量找的候选，要严格复现用 `--reingest` 强制重建）。
- **同一份记忆换回答模型**：把 `stores/` 复制到新的输出目录，只改 `roles.chat` 再跑，抽取全部复用，只重跑回答。
- **字段白名单**：`question_type`、`answer`、`has_answer`、`answer_session_ids` 不会进入记忆系统，见 `src/memsys/bench/longmemeval.py` 和对应测试。
- **泄漏检查**：`memsys bench leakcheck --data data/longmemeval_oracle.json` 检查代码、文档、测试里有没有题号、题目原文、标准答案，以及提示词里的名称和例句是否出自评测数据。改提示词后跑一次；本地有数据时测试也会跑它。
- **对照组**：`--mode full-context`（全量拼接）和 `--mode no-memory` 只用于对比，不能拿去提交。
- **追问错题**：`memsys bench load --data ... --ids <题号>` 把某道题的历史灌进对话入口，然后按提示用 `memsys chat` 当场追问，配合 `/why` 看它检索到了什么。重复执行不会重复载入。
- **检索规模**：报告里有每题的记忆条数和分阶段耗时；`memsys bench scale --out runs/scale.md` 在合成数据上测检索延迟随记忆条数（默认 100 到 1 万条、1024 维向量）的变化，不调模型。

### 评测前检查与报告口径

先运行 `memsys doctor --check` 验证配置中的模型和 embedding 确实可调用；任一实际调用失败时返回非零退出码。
账户余额不足或服务未启动时先恢复服务，再启动新版本的全量评测：

```bash
memsys doctor --check
memsys bench run --data data/longmemeval_oracle.json --out runs/oracle-v3 --workers 2 --reingest
```

报告分别统计已回答、失败、未开始和未评分题数，准确率只包含当前数据集中有答案且已评分的题。
本地模型评分、小样本评分和离线规则评分均为实验结果；正式成绩以项目组对全量 500 题统一复跑为准。

按题型抽取代表题时，使用固定历史长度分位点选择六类各5道有答案题，并加入可用题型的拒答样本；不按旧答案或分数挑题。34题基线18/34正确，后续修复独立复测，完整结果见 [代表题评测与修复报告](runs/representative-fixes-v15/assessment.md)。小样本结果不能外推全量准确率，各版本答案不能合并为一个新分数。

```bash
python3 scripts/select_representative.py --data data/longmemeval_oracle.json --out runs/representative-new
memsys bench run --config config.json --data runs/representative-new/sample.json --out runs/representative-new/oracle
memsys bench judge --config config.json --data runs/representative-new/sample.json --hyp runs/representative-new/oracle/hypotheses.jsonl
memsys bench report --data runs/representative-new/sample.json --run runs/representative-new/oracle
```
本地评分只接受已完成的yes/no标签；预算为256，截断或非标签输出只修正一次，仍失败的题保持未评分。结果保存实际评分模型与输出状态，避免把无效响应记成答错。

针对跨会话薄弱项的迭代，先审阅现有错题并做针对性回归；只有实质改动或回归风险需要确认时才启动整批评测。按当前要求默认抽40题：跨会话16题、知识更新8题、时间推理8题，其他三类共8题。保留固定题号做同题比较；新样本按类型及固定种子选择，过程不读取正确性标签。每轮冻结源码、提示词、模型和配置，关闭模型自动回退；全部答题完成后才启动评分。`--sample-size 100` 仅保留旧批次兼容，后续采用默认40题。

```bash
python3 scripts/run_accuracy_iteration.py --out runs/accuracy-new \
  --paired runs/accuracy-v46-necessary40/sample40.json --seed memory-accuracy-new --workers 6 --stage all
python3 scripts/analyze_accuracy_iteration.py --out runs/accuracy-new \
  --paired-run runs/accuracy-v46-necessary40/current
# 中断后继续同一冻结版本；保留已完成答案，只重建未完成题
python3 scripts/run_accuracy_iteration.py --out runs/accuracy-new --stage all
```

`freeze.json` 保存来源指纹、样本文件及阶段记录，`assessment.md` 与 `answers-and-audit.json` 列出完整错题、回退题和执行原因。重新抽取存在模型波动，评分器与回答器相同时也缺少独立判断；40题或历史100题加权诊断结果不能视为正式全量成绩。

只改读取逻辑时，可以冻结并复用同一批已完成的记忆库，隔离写入波动。先用 `prepare` 冻结新版本，再运行缓存对照：

```bash
python3 scripts/run_accuracy_iteration.py --out runs/accuracy-read-new \
  --config config.json --paired runs/accuracy-v46-necessary40/sample40.json --stage prepare
python3 scripts/run_cached_read_iteration.py --out runs/accuracy-read-new \
  --seed-run runs/accuracy-seed
python3 scripts/analyze_accuracy_iteration.py --out runs/accuracy-read-new \
  --paired-run runs/accuracy-seed/current
```

缓存对照要求种子运行全部完成；当前样本可以是它的固定子样本，每道题的完整输入必须一致，写入模型、写入参数、向量参数及写入源码指纹也必须一致。程序复制不可变种子库，验证每题缓存命中后才评分；40题与100题分别保留实际分母，配对只比较共同题目。不同写入版本必须重新建库。独立样本使用 `--exclude-sample <旧样本>`，避免与优化样本重叠；历史未答比例单独记录，不能称为盲测。中断恢复脚本 `scripts/resume_accuracy_iteration.py` 会等待原进程或指定种子完成，只重试未完成阶段，账户错误停止重试。

上下文比例统计“检索记忆 + 问题”，另列包含系统规则的完整回答提示词估算量。工作记忆预算包含日期和标签，
事实保持完整，超限时移除低优先级条目；当前查询取版本链末端，历史查询保留命中的旧版本及最新版本。
`/why` 与 trace 的 `omitted` 字段可检查受预算、版本或条数限制而未选入的条目。

当前抽取直接读取整批完整用户和助手消息，参考 Mem0 的单次抽取流程，不按固定字节或 token 数切块，也不因输出异常拆小重抽。
旧 `extract_chunk_tokens`、`extract_full_rounds`、`extract_assistant_chars`、`extract_context_tokens` 配置已退役；输入限制由所接模型接口处理。`memory_input_budget_tokens` 不限制抽取输入，`extract_max_tokens` 只限制输出。

默认回答输出为8192 token，触顶可增至 `answer_max_tokens_limit=32768`；抽取从 `extract_max_tokens=65536` 起，触顶可增至 `extract_max_tokens_limit=131072`。最多进行三次完整生成；拒答、鉴权和内容错误不会触发输出扩容。模型配置的 `output_token_headroom` 另外预留推理token，每次真实调用均记入用量账本。完整回答输入预算默认131072，工作窗口默认12000，单位遵循 `budget_mode`；默认 `utf8` 时是字节，不能当成模型token。输入和输出预算按服务商实际支持范围配置。

WebChat 与公共评测宿主直接生成用户可见正文，不要求证据清单、计算审计或 `advice_task`，不再生成或解析内部回答信封。模型继续收到个性化、角色状态区分与缺少前提时保留不确定性的指令；宿主只检查正文非空与生成完成，不复查或改写语义。输出扩容尝试与用量仍正常记录。抽取覆盖报告每个非空角色一条；遗漏最多4个短摘录，采样时明确标记 `omissions_complete=false`，它不证明语义完整。

本机BGE嵌入配置 `embed_max_input_bytes=4096`。较长的事实、查询和其他嵌入输入按UTF-8字节完整分段，逐段编码后按片段字节权重合成单位向量；短输入沿用原向量。原始事实正文、结构值及顺序不变，分段不会增加事实条数。该选项仅限制每段编码输入，不代表把模型的固有窗口调大；模型清单记录分段与合成策略，调用账本包含全部片段。分段后的检索质量需单独评估。
`episode_chunk_vectors=true` 将完整原文按说话人分段编码，单段受 `embed_chars=2000` 与 `embed_chunk_tokens=384` 约束。
每段保存原角色及字符区间；检索按每轮最高片段相似度排名，同一轮的多个片段只贡献一次排名分，避免长文本因片段多而加分。
`query_focused_excerpts=true` 让重排和回答优先保留语义命中片段，并结合关键词选择窗口，保留说话人、原文顺序与省略标记。
旧库可直接读取，自动增加分段表，但原有前缀向量不会自动重算；执行 `memsys reindex --config config.json --user <用户> --data-dir <原库目录>` 后才获得全原文索引。
重建先完成全部向量调用，再原子替换；失败保留旧索引，不重新抽取事实。`/stats` 和 trace 的 `index_coverage` 显示分段与旧前缀的覆盖数。
抽取分块及助手前缀截断开关已经退役。向量索引仍支持 `episode_chunk_vectors=false` 的旧前缀格式；切换索引格式需要重建。
完整分段会增加向量输入量、存储和检索计算，实际耗时与费用由 embedding 模型和输入长度决定。
旧报告中的抽取分块/截断字段仅保留格式兼容，当前流程始终为零；调用与输入计数用于记录，不据此自动分块。

`aggregation_validation=true` 在内置 `respond()` 与回答行为回归中检查回答模型提交的 `CALCULATION` JSON：支持同单位数值求和、明确名称或物品的去重计数，以及明确群体数量求和。
程序核对选入上下文的精确引文、数字与单位的邻接、显式重复、事实与来源原文交叉重复、十进制计算以及答案中的首个数字。
未通过时只修正一次；仍未通过或修正调用失败时，最终回复说明无法核实合计，原候选答案保存在 trace 的 `aggregation.rejected_answer`。
没有凭据、操作不支持和关闭核验分别记录为 `not_provided`、`unsupported`、`disabled`；`checks_passed` 仅表示机械检查通过，不能证明事件选取、日期关系或召回完整性正确。
群体数量的数字或英文零至九十九必须紧邻引文中的群体名称；对象名称计数无需杜撰一个数值。日期间隔提交date_difference凭据，由程序核验实际供给的日级来源日期、可信TODAY、请求单位及完整日/周/月/年计算；月度或模糊日期不能补造具体日。来源中的 today/yesterday 等以真实会话日期为锚点；明确的“两周之前”也可作为两个行动间的关系凭据，由程序核验原文、方向和行动关联，模糊偏移不当作精确天数。历史事件之间的间隔使用各自来源日期，相对现在的间隔使用调用方TODAY。所有模型调用与用量均进入账本。
活动耗时求和也接受原文中的精确日期区间和秒、分、小时、天、周换算；日期端点和连接词必须来自同一精确引文，缺失年份只使用该来源日期，模糊月份不补造日期。数量凭据中的英文零至九十九会规范为十进制数，仍核对完整名词短语与最终答案。
`semantic_validation=true` 默认先区分查值、最新状态、历史状态、前后比较、求和、计数、列表、时间间隔与建议。写入保留对象、发生次序、条件及累计/增量标注；不同对象和条件不能仅因属性槽相同而覆盖。数值聚合逐页读取后，全局区分独立事件、重复描述及累计快照，程序检查记录分组、最新快照、字面限定条件和算术。分页引用支持行号，由程序复制原文，避免模型改写引文。
普通内置回答增加一次相关性及个人事实支持复核，收到与回答相同的来源、日期及条件；失败只修复一次，并保留拒绝答案与原因。复核与跨页判断仍使用配置的回答模型，不能作为独立正确性证明。超出完整证据预算或服务失败时保留失败状态，不截断证据拼凑总数。评测缓存同时核对原始会话内容；固定样本回归冻结源码、配置及样本，写入变化时重建，纯读取变化复用固定库，关闭自动切换模型，失败保留在原分母。
分页阶段收集可提供任一对象、时间或状态关系的证据，完整限定条件留到跨页汇总时判断。查询中的并列对象不能被截成只限制其中一方的强制条件。已选原文会补入同会话开头、前一轮和当前轮的用户文本以解析指代；补入内容仍按来源引用并受预算限制。汇总预算计入问题及全部系统规则，超限时只接受来源内精确连续子串压缩。事实的更新关系尚未确认不等于用户陈述本身不确定，判断仍需核对原始陈述与后续更新。
跨页证据过多时先在各批内保留精确连续引文，再执行全局条件和状态判断。引文说话人由原文核对，助手提供的通用估价不能替代用户记录的个人数值；差值问题保留两个原始操作数，普通建议不要求提交无关的计算凭据。

明确询问多个活动的固定时长合计时，查询规划使用求和并保留全部来源；“一周半”按1.5周验证，缺少计算凭据不能直接给出数字。
日期间隔的结果必须是纯数字。日期端点可请求程序复制整个来源，但事件短语必须在正文中逐字存在，日期仍须满足完整日期或受支持的相对时间校验；记录日期不能自动充当事件日期。

`complete_episode_context=true` 启用完整原文合成候选路径：检索覆盖完整、所有候选事实可追溯到已选原文、完整原文及全部系统消息均符合预算时，按会话和轮次顺序直接供给这些原文，不先逐页摘要。任一条件不满足就沿用原有分页路径。trace 的 `aggregation.mode=complete_original_context` 保存实际输入原文、来源覆盖及预算；算术凭据和语义复核仍执行。该路径只包含检索规则选中的原文，完整历史诊断脚本 `scripts/run_full_context_iteration.py` 的分数另行报告，不能记作记忆系统正确率。

历史状态区分当前值及最近一次变更前的值，相对日期以陈述来源日期为锚点。问题对象标签与唯一相关记录存在偏差时，回答可保留原始细节并明确未确认的标签，不能将数值迁移到另一对象或补造日期。
未指明日历单位或两个日期端点的活动耗时不强制使用日期相减。纠错请求不重复携带旧草稿；原提示接近输入上限时，用全部已供给证据、原问题、调用方日期与相同计算规则构造较短请求，仍超限则拒绝纠错，不截断证据。明确序号查询优先保留原文中对应的编号条目；不推断无编号列表的次序。
`recall()` 交给外部 agent 后无法观察其生成结果，自动核验只适用于内置回答；外部调用方可用 `memsys.aggregation.inspect_calculation()` 核验自己的输出。


当前系统工程能力与维护流程见 [系统缺口工作流程](docs/system-gap-workflow.md)：

- 预算默认按 UTF-8 字节保守限制内容；可用本地 tokenizer（安装 `strict` 可选依赖），记录计数口径及文件指纹。完整回答和记忆模型消息有输入上限，服务端实际 token 用量仍取 API usage。
- 同单位求和、明确名称去重计数及群体数量支持分页聚合，每页记录来源覆盖，事实与原文重叠时只让原文贡献；超限或核验失败拒绝完整合计，外部 agent 可用 `recall_pages()`。
- 需要完整覆盖的列表或对比问题使用分页精确引文提取，再在内容与完整消息预算内回答；来源或引文不合法、完整证据超预算时拒绝完整回答。
- `memsys repair` 预览并按明确会话或失败日志排队旧遗漏任务，之后执行 `rebuild`；不会仅凭“没有事实”自动补偿。
- 细粒度遗忘需唯一原文引文与可核验字段值，生成安全视图并移除旧索引；证据不足继续整轮屏蔽。
- `memsys cleanup` 默认预览，`--plan <预览文件> --apply` 在校验计划和备份后物理清理。有效版本链保留空载荷墓碑，备份仍含原文。数据库分配页默认256 MiB，WAL与备份单独管理。
- 安装 `fast` 可选依赖后自动使用 NumPy 精确矩阵打分；仍是线性扫描。统一验收加入600周期持续运行、完整依赖测试及加速排序核对：

```bash
PYTHONPATH=src python3 scripts/validate_memory_system.py --config config.json \
  --system-gaps --extended-python '<可选依赖虚拟环境>/bin/python' --full --out runs/system-gaps-full
```

在不调用模型、不写库的情况下检查所有历史轮次的抽取与向量分段覆盖；空输出替身只验证输入边界，不能作为准确率结果：

```bash
PYTHONPATH=src python3 scripts/check_write_coverage.py --data data/longmemeval_oracle.json --config config.json --out runs/write-coverage
# 旧助手前缀策略的对照
PYTHONPATH=src python3 scripts/check_write_coverage.py --data data/longmemeval_oracle.json --config config.json --prefix --out runs/write-prefix
```

真实回答模型可用后，先运行 10 个自拟固定记忆案例，检查事件求和、累计值、数量、已支付的未来活动、推荐范围、
用户实际行为、时间参照、日期精度和历史版本。用户指令要求精确简短输出，检查器只比对这一输出，不做模糊语义评分。

报告记录完整案例、模型和源码指纹，分别统计答错、调用失败与未开始，账户异常时停止；不支持 `--fake`，这组回归不替代完整检索、抽取或 LongMemEval 评测：

```bash
memsys bench behavior --config config.json --out runs/behavior-v3
```

`scripts/check_date_semantics.py`另验证来源标题与当日事件的连续引用、两个历史事件的日期间隔，以及真实TODAY与记录日期的区别。使用固定自拟记忆与真实回答模型，不经过抽取，不代表基准准确率；完整验收把这三项作为全量回放的前置检查。

原始会话的端到端机制回归包含 12 个自拟案例，实际调用抽取、裁决、检索和回答模型，检查独立事件与累计状态、当前与历史版本、计划、支付与活动日期、推荐范围、日期精度、实体别名和共享原文遗忘。参考结果只在回答完成后使用，调用失败与答错分开记录；精确输出检查是回归门槛，不替代统一语义评分。

```bash
memsys bench mechanisms --config config.json --out runs/mechanisms-v1
# 按依赖顺序执行完整验收；模型不可用时记录阻塞并跳过依赖阶段
PYTHONPATH=src python3 scripts/validate_memory_system.py --config config.json --out runs/reliability-current --full
```

执行顺序、验收条件和未完成项见 [记忆系统可靠性工作流程](docs/memory-reliability-workflow.md)。只做本地检查时去掉 `--full`；加 `--semantic` 检查真实 embedding，加 `--with-models` 运行真实回答和机制回归。每个验收目录保存阶段日志、源码指纹和状态；源码变化后使用新目录，保留旧结果供对照。

也可以单独检查配置中的真实 embedding：以下用自拟事实和替身抽取在临时内存库验证中文查询、英文改写、长原文两种角色的尾部召回与向量维度，并与关闭分段后的同库检索比较。
它不生成回答或评分；embedding 费用按配置记账，使用本地 bge-m3 时显式为免费。

```bash
PYTHONPATH=src python3 scripts/check_semantic_retrieval.py --config config.json --out runs/semantic-smoke
```

已有哈希向量记忆库可做不调用模型的预算回放；脚本在只读副本上工作，关闭查询分析和重排，输出预算检查结果，不能用它判断回答准确率：

```bash
PYTHONPATH=src python3 scripts/check_retrieval_budget.py --data data/longmemeval_oracle.json --stores runs/oracle-v2/stores --out runs/budget-validation
```

## 功能检查

本轮新增检查使用合成数据和固定替身，验证存储与 HTTP 契约，不评价模型质量或检索速度：

```bash
python -m unittest tests.test_qdrant_storage tests.test_memory_http tests.test_qdrant_migration tests.test_remote_rerank
```

真实服务器检查仅支持临时本机 Qdrant，使用独立集合并在结束时删除测试集合；不允许指向云端或生产服务器：

```bash
QDRANT_TEST_URL=http://127.0.0.1:16333 python -m unittest tests.test_qdrant_server
```

旧 SQLite 功能测试和评测套件本轮未执行。不要将旧测试数量或历史分数当作这次构建的检查结果。

## 目录

```
src/memsys/
  llm.py         模型适配层：OpenAI 兼容接口、函数适配器、重试、JSON 解析、用量与费用账本
  anthropic_client.py  Anthropic Messages API（官方 SDK，可选）
  discover.py    找模型：配置 → 宿主 → 环境变量 → Ollama → 降级
  config.py      模型角色与全部记忆参数
  store.py       Qdrant 主存储：日志、事实、来源、版本、提交、游标、任务
  qdrant.py      Qdrant 服务器 REST 客户端
  client.py      Agent 域名客户端
  agent_adapter.py 示例宿主 Agent，无答案复查
  write.py       写入路径：轮次、门控、抽取、补抽
  update.py      确定性的精确文本去重与新增；无模型调用
  read.py        读取路径：查询分析、混合检索、预算选取、渲染
  lifecycle.py   巩固与遗忘
  core.py        对外接口 MemorySystem（recall / respond / rebuild …）
  serve.py       代理与 /memsys HTTP 接口
  mcp.py         MCP server
  hook.py        Claude Code 的 hook
  cli.py         全部命令
  fake.py        离线替身模型
  bench/         LongMemEval 加载、回放、本地评分、报告、检索规模测试
docs/            架构设计文档、流程图、答辩演示脚本
tests/           单元测试与集成测试
```

## 参考

- Di Wu et al. LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory. ICLR 2025. [arXiv:2410.10813](https://arxiv.org/abs/2410.10813) · [代码](https://github.com/xiaowu0162/LongMemEval)。本地评分的提示词取自其 `evaluate_qa.py`（MIT License）。
- 主流程参考 Mem0 的整批单次抽取、新增记忆与检索分工；检索返回来源，宿主负责生成。旧研究和实验记录保留作历史资料，不决定当前选型。

## 历史检索和分块实验记录

此节描述存储迁移前的实验，不作为当前架构或部署说明。当前实现以架构文档及 `deploy/` 为准。

默认采用普通角色分块加本地 BGE 重排。父子分块和语义边界实验未获得稳定质量收益，已从当前运行代码、配置和新数据库结构移除；历史实验源码和结果保存在 `runs/rag-refactor-v17`。实测依据见 [重构评估](docs/rag-refactor-assessment.md)，历史设计与当前决策见 [实施计划](docs/rag-refactor-plan.md)。

```bash
python -m pip install -e '.[strict,fast,rerank]'
```

`memory.rerank_backend=bge` 为默认；`fusion` 用于离线机制检查和明确降级，`llm` 用于历史对照及已有接入兼容。远程模型和 tokenizer 使用固定 commit revision，惰性加载，进程内同配置复用一个推理队列。长候选按 reranker 配对 token 预算分窗，包含命中位置、原始角色、日期及助手回答的对应提问。`bge-m3` 负责向量召回，`bge-reranker-v2-m3` 负责重排，两者各自独立。

跨设备默认使用 CPU/float32。本机 `config.json` 使用已验证的 MPS/float32、batch 4、`rerank_strict=true`，加载或推理失败直接报告错误。非严格模式可记录失败并保留融合排名；质量对照必须严格，失败不能算作 BGE 成功。项目 `.venv` 已安装本地模型依赖，可执行 `source .venv/bin/activate` 后运行 `memsys doctor --check` 验证实际后端；`--fake` 显式使用融合排名，不下载或调用模型。

```bash
memsys reindex --config config.json --user demo
memsys reindex --config config.json --user demo --rollback
```

重建索引复用同一向量空间的事实，校验来源、原文覆盖、向量维度和 FTS 后事务切换；失败保留旧索引。旧父子实验库须显式重建为普通角色分块，事务同时移除废弃索引表和列。旧配置里关闭的实验参数可告警后读取，启用父子/语义分块会直接报错。

数据库保留当前及前一检索索引代，回滚须使用相应分块配置与代码版本；当前代码拒绝恢复已退役父子代，恢复历史实验需使用冻结源码。事实、原文或遗忘状态变化后拒绝恢复旧代；物理清理会删除索引回滚载荷。保留索引代占用额外存储，`stats.storage.retained_index_payload_bytes` 单独披露。

`query_policy=on_risk` 让简单查值先直接检索；完整聚合、日期、历史状态或低召回置信度仍可调用规划模型。`answer_review_policy=always/on_risk/off` 仅控制回答复核，操作语义、跨页处理和程序校验分别配置，不能通过关闭回答复核连带关闭分页合并。两项按需策略需要独立对照才能切换默认值。

冻结同一事实库的检索隔离评测可用 `scripts/evaluate_retrieval_variant.py`；它只复制记忆库，不复制旧答案、标签或 trace，生成完成后才调用固定 judge。正常运行的模型候选池保持原配置；质量对照禁止自动切换。更换事实向量空间会影响更新裁决，正式端到端评测会重新抽取建库。

本次采用与清理已通过 345 项扩展回归及全部 9 个工程验收阶段。12 组自拟样例、14 个问题、284 个原文块的分块内容、BGE 输入和最终记忆上下文在清理前后完全一致；这属于机制等价检查，不是新的正确率评测。真实父子实验库的独立副本已验证事务失败和中断后保留旧索引、成功迁移后原文及事实状态不变、事实向量逐字节复用。实际中文端到端样例使用 MPS/float32 的 BGE，未降级、没有云端重排调用。清理前恢复快照和验证明细保存在 `runs/bge-adoption-v18`。
