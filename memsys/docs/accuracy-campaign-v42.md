# 记忆正确率迭代记录

每轮保留冻结源码、配置、固定题号、原始答案、评分及错误日志。未完成全部题目和评分的运行不报告正确率。历史批次为100题；按用户后续要求，新增整批评测默认40题且只在必要时启动。

| 运行 | 已答 | 完整正确数 | 跨会话 | 知识更新 | 时间推理 | 评估路径 |
|---|---:|---:|---:|---:|---:|---|
| [accuracy-v21](../runs/accuracy-v21/assessment.md) | 100 | 53/100 | 16/40 | 11/20 | 11/20 | fresh_ingestion |
| [accuracy-v22](../runs/accuracy-v22/assessment.md) | 100 | 55/100 | 17/40 | 13/20 | 7/20 | fresh_ingestion |
| [accuracy-v23](../runs/accuracy-v23/assessment.md) | 100 | 61/100 | 20/40 | 12/20 | 12/20 | fresh_ingestion |
| [accuracy-v32-longcat-reader](../runs/accuracy-v32-longcat-reader/assessment.md) | 100 | 70/100 | 28/40 | 14/20 | 11/20 | paired_reader_on_identical_stores |
| [accuracy-v34-step-context](../runs/accuracy-v34-step-context/assessment.md) | 100 | 78/100 | 32/40 | 16/20 | 13/20 | paired_reader_on_identical_stores |
| [accuracy-v35-bounded-selection](../runs/accuracy-v35-bounded-selection/assessment.md) | 100 | 72/100 | 29/40 | 16/20 | 12/20 | paired_reader_on_identical_stores |
| [accuracy-v36-speaker-and-difference](../runs/accuracy-v36-speaker-and-difference/assessment.md) | 100 | 82/100 | 33/40 | 18/20 | 14/20 | paired_reader_on_identical_stores |
| [accuracy-v37-larger-context](../runs/accuracy-v37-larger-context/assessment.md) | 100 | 75/100 | 29/40 | 15/20 | 14/20 | paired_reader_on_identical_stores |
| accuracy-v38-full-context-reference | 100 | 89/100 | 37/40 | 17/20 | 16/20 | full_context_diagnostic |
| [accuracy-v39-complete-context](../runs/accuracy-v39-complete-context/assessment.md) | 100 | 79/100 | 32/40 | 15/20 | 13/20 | paired_reader_on_identical_stores |
| [accuracy-v40-fresh-complete-context](../runs/accuracy-v40-fresh-complete-context/assessment.md) | 100 | 80/100 | 31/40 | 19/20 | 13/20 | fresh_ingestion |
| accuracy-v41-full-evidence-budget | 0 | 未启动，已停止 | — | — | — | fresh_ingestion |
| accuracy-v42-qualified-recall | 0 | 未启动，已停止 | — | — | — | fresh_ingestion |
| accuracy-v43-low-read-reasoning | 0 | 未启动，已停止 | — | — | — | fresh_ingestion |
| accuracy-v44-selective | 0 | 未启动，已停止 | — | — | — | fresh_ingestion |
| accuracy-v45-selective-low | 0 | 未启动，已停止 | — | — | — | fresh_ingestion |
| [accuracy-v46-necessary40](../runs/accuracy-v46-necessary40/assessment.md) | 40 | 30/40 | 11/16 | 7/8 | 5/8 | paired_reader_on_identical_stores |
| [accuracy-v47-calculation-contract40](../runs/accuracy-v47-calculation-contract40/assessment.md) | 40 | 31/40 | 11/16 | 7/8 | 6/8 | paired_reader_on_identical_stores |
| [accuracy-v48-category-evidence40](../runs/accuracy-v48-category-evidence40/assessment.md) | 40 | 30/40 | 11/16 | 7/8 | 5/8 | paired_reader_on_identical_stores |
| [accuracy-v49-grounded-quantities40](../runs/accuracy-v49-grounded-quantities40/assessment.md) | 40 | 33/40 | 13/16 | 8/8 | 5/8 | paired_reader_on_identical_stores |
| accuracy-v50-bounded-recovery40 | 0 | 未启动，已停止 | — | — | — | fresh_ingestion |
| [accuracy-v51-literal-counts40](../runs/accuracy-v51-literal-counts40/assessment.md) | 40 | 34/40 | 13/16 | 8/8 | 7/8 | paired_reader_on_identical_stores |
| accuracy-v52-calendar-precision40 | 0 | 未启动，已停止 | — | — | — | fresh_ingestion |
| accuracy-v53-preference-context40 | 0 | 未完成 | — | — | — | fresh_ingestion |
| accuracy-v54-completed-event-bounds40 | 0 | 未完成 | — | — | — | fresh_ingestion |
| [accuracy-v55-verified-date-format40](../runs/accuracy-v55-verified-date-format40/assessment.md) | 40 | 35/40 | 15/16 | 8/8 | 6/8 | paired_reader_on_identical_stores |
| accuracy-v56-requested-bounds40 | 0 | 未完成 | — | — | — | fresh_ingestion |
| [accuracy-v57-bound-schema40](../runs/accuracy-v57-bound-schema40/assessment.md) | 40 | 36/40 | 14/16 | 8/8 | 6/8 | paired_reader_on_identical_stores |
| accuracy-v58-complete-weeks40 | 10 | 未完成 | — | — | — | paired_reader_on_identical_stores |
| accuracy-v59-cumulative-index40 | 4 | 未完成 | — | — | — | paired_reader_on_identical_stores |
| [accuracy-v60-aggregate-breakdown40](../runs/accuracy-v60-aggregate-breakdown40/assessment.md) | 40 | 36/40 | 14/16 | 7/8 | 7/8 | paired_reader_on_identical_stores |
| accuracy-v61-count-scope40 | 0 | 未完成 | — | — | — | fresh_ingestion |
| [accuracy-v62-independent40](../runs/accuracy-v62-independent40/assessment.md) | 40 | 35/40 | 13/16 | 8/8 | 7/8 | paired_reader_on_identical_stores |
| [accuracy-v62-literal-group-units40](../runs/accuracy-v62-literal-group-units40/assessment.md) | 40 | 38/40 | 15/16 | 8/8 | 7/8 | paired_reader_on_identical_stores |
| [accuracy-v63-mixed-evidence40](../runs/accuracy-v63-mixed-evidence40/assessment.md) | 40 | 35/40 | 14/16 | 7/8 | 7/8 | paired_reader_on_identical_stores |
| accuracy-v64-attendance-year40 | 0 | 未完成 | — | — | — | fresh_ingestion |
| [accuracy-v65-possession-continuity40](../runs/accuracy-v65-possession-continuity40/assessment.md) | 40 | 35/40 | 14/16 | 8/8 | 5/8 | paired_reader_on_identical_stores |
| accuracy-v66-field-specific-repair40 | 0 | 未启动，已停止 | — | — | — | fresh_ingestion |
| [accuracy-v67-read-transport-recovery40](../runs/accuracy-v67-read-transport-recovery40/assessment.md) | 40 | 34/40 | 13/16 | 7/8 | 6/8 | paired_reader_on_identical_stores |
| [accuracy-v68-bounded-endpoint-backoff40](../runs/accuracy-v68-bounded-endpoint-backoff40/assessment.md) | 40 | 39/40 | 16/16 | 8/8 | 7/8 | paired_reader_on_identical_stores |
| [accuracy-v68-regression40](../runs/accuracy-v68-regression40/assessment.md) | 40 | 33/40 | 12/16 | 7/8 | 7/8 | paired_reader_on_identical_stores |
| accuracy-v69-propagate-read-transport40 | 0 | 未完成 | — | — | — | paired_reader_on_identical_stores |
| accuracy-v69-second-regression40 | 0 | 未完成 | — | — | — | paired_reader_on_identical_stores |

## 当前候选状态

候选：`accuracy-v78-count-field-feedback`；正式首组：`accuracy-v78-fixed-first24`。按用户最新要求，在 Step 多轮受限冒烟出现服务故障后固定使用 GPT-6 Luna。511项全量测试、12项能力回归、157条历史错误回放、完整题库泄漏检查和隔离安装构建验证通过。三题历史库读取回归3/3，三个真实写入机制案例3/3；用户已调整为六类能力各4题、每批24题；两组样本已冻结且不重合，现正重建首组记忆库，尚无正式24题分数。[当前执行报告](gpt6luna-memory-execution.md)。

候选源码：`d91613e50490a57a1f52dae9d1ee8d849cdd4738e1cfdff07e25e87620a306d8`；冻结配置：`003fd5ce293931000c453334a30e1184eb35a286c79331f36ad86e77fe496214`。所有旧答案、Boolean标签及冻结候选均保留。

## 历史v47采用记录

采用v47日期与时长修复版，实际config.json已切换到冻结验证的LongCat写入、Step查询/回答/评分配置及本地BGE检索。撤回v48新增的分类汇总提示；两版源码、配置、原始答案和逐题审计均保留。

同一40题、同一冻结记忆库：v40对应子集30/40，v46为30/40，v47为31/40，v48为30/40。v48相对v47新增正确1题、回退2题，没有净收益。v47的跨会话为11/16、知识更新7/8、时间推理6/8；以上均为原始自动评分。

采用后重新构建通过，388项单元测试通过；当前配置实际调用的记忆、回答、1024维embedding及MPS BGE重排全部正常。v46实际机制检查8/8，v47日期/时长检查2/2通过；未采用的v48分类汇总检查1/1通过。全部40题回答、Boolean评分和缓存命中均完整。 项目评测数据泄漏检查通过。

v47有1题回答数字与参考数字不同但自动评分为正确，已单独标记待人工复核，未修改原始评分。因此31/40不能作为稳定净提升的证明。仍有序数数量、缺少年份的日期、范围辨认和生成不完整等问题；未达到100%。后续仅在明确需要验证时使用约40题，不继续未启动的100题参数探索。

该采用记录源码：`36b787d135d713e2e38ae9fc734fb6d17de95f10bf4963147665160d1148af32`；配置：`641da23e8c85e2b11128fafa247abfc655ec8a1db93f7547d5bdcb1e04e70ab6`。


## 比较口径

- 优化样本共100题：跨会话40、知识更新20、时间推理20、用户事实8、助手事实6、偏好6。它是Oracle诊断样本，不能外推正式500题成绩。
- v21–v23原始分数使用GPT评分；v23同一批答案用Step重新评分为62/100。之后读路径对照使用固定Step评分，跨评分模型的分数不能直接归因于代码。
- v32–v39使用优化样本。缓存对照复用同一不可变写入库，并要求100次写入缓存命中；不同回答模型、预算或源码同时变化时，不单独归因于一个改动。
- 同模型、同预算的v34/v35对照为78→72，新增正确2题、回退8题，未证明新筛选逻辑带来稳定收益。8个回退题均没有进入有界压缩路径；39个完全相同的答案评分一致，因此也不能把这些回退直接归因于未执行的压缩代码。
- v40–v45使用另一批100题，与优化样本零重叠。v40从空库写入；v41–v45复用v40的冻结库。该样本首次选定时48题没有历史答案，其余52题曾在历史运行出现，属于独立诊断样本而非盲测。
- v44/v45保存了精简证据候选配置：证据8KB、输入16KB，关闭完整原文路径；原100题计划已停止。v46沿用v44配置，改为40题必要验证。
- 用户后续要求只在必要时评测。未启动的v41/v42/v43/v45预算与推理参数探索已停止，冻结配置保留，不产生分数或采用结论。v41已完成的7个机制用例保留。
- v48只调整离散类别合计的凭据选择提示词；原有字面quantity_label、来源、重复、数量与算术校验均保留。与v47同题同库同模型同预算，并用1个自拟案例验证。
- v49修正剩余量、完成进度序数、记录年份及跨轮活动条件，仍用同一40题和记忆库。自动评分为33/40，跨会话13/16、知识更新8/8、时间推理5/8；相对v47新增正确3题、回退1题。数字标签异常另记，保留原始评分。
- v50针对已观察到的截断和凭据失败，增加包含推理额度的有界重试、类别数量修正、明确来源年份、显式近似月数及字面商品识别。读取与查询使用同一Step模型的low推理参数；评分与写入配置保持原样，因此结果是整组修复及读取参数的对照，不能单独归因于其中一项。自拟读取检查不包含记忆写入，不能替代端到端准确率。
- v50自拟读取检查暴露了序数合计仍不通过，因此在题库评测启动前停止，没有40题分数。其原始名称出现检查将一个以拒答开头的商品描述判为通过，另存审计注明该检查不能证明预期识别行为。v51修正英文规则复数、明确累积序数的字面求和和商品识别表达，并仅重跑受影响的自拟案例。
- v51完整评分34/40（85%）：跨会话13/16、知识更新8/8、时间推理7/8。相对v49新增正确5题、回退4题；约数合计、年份合计和两道偏好题仍需处理。
- v52保留合法数值凭据供语义修正、记录修正失败的原始凭据，新增仅有年份的近似区间及from/to路由，恢复读取模型的默认推理。年份自拟检查通过，约数请求发生连接中断；两道旧偏好题的上下文复查均被自动评分为错误，因此在40题启动前停止。原始失败、补测与评分分别保留。
- v53在原文旁为建议类问题添加预算允许的用户原话索引，并检查具体格式、平台、排除条件及近况；索引保留原角色和计划措辞，超过预算便省略索引，完整原文仍保留。先复查四道受影响旧错题，开发子集不作为整批或独立样本分数。参考答案中缺少原文支持的个人经历另存疑点，不补写入记忆。
- v53开发子集评分3/4，两道偏好和约数合计通过；年份题的引用与10年计算已通过来源检查，却因答案漏写近似词被拦截。因此不再启动该版本40题，修复后合并验证。
- v54允许原始用户已完成事件的记录日仅作为最晚发生日，结合明确终点算出至少多少天，并保留确切间隔未知。自拟下界案例通过；旧时间错题仍评分为错误，原始标签不改。小预算修正测试暴露规则占用过多后，压缩规则并重建，425项测试通过。
- v55仅在来源、运算、结果已通过且唯一问题是缺少精度限定词时，给裸数值或单位答案补近似或下界措辞。保留修正前答案，复查同一凭据，再做语义核对；数值错、来源错、单位错、精确区间冲突及实质文本均不能走格式修正。432项测试通过；旧年份凭据的真实模型复核单独记录，不当作新题正确率。
- v55完整评分35/40（87.5%），跨会话15/16、知识更新8/8、时间推理6/8。相对v51新增正确3题、回退2题。下界引导干扰普通月份估算；商品识别额外说明品牌未记录；一处建议修正丢失已支持的条件分析。完整原始标签和失误日志均保留。
- v56把下界限制到明确请求的最少天数/周数，普通月份/年份题允许根据刚完成的措辞给近记录日期的估算；商品识别直接给已知描述和来源，建议修正保留支持的内容并处理未支持的个人行动。433项测试通过，三道开发错题全部评分通过；自拟最少天数检查因字段放到端点而失败，所以没有启动40题。
- v57明确下界字段位于外层计算对象，并让格式错误指出具体位置。原始来源、请求范围、时钟、数值与语义校验不变；不把无效凭据自动规范化为通过结果。补测回答至少21天，来源、算术及语义核对均通过；自拟脚本因额外要求准确间隔未知的文字仍判失败，原始结果和启动理由分别保留。合并验证沿用同一40题；本地嵌入服务恢复及未完成题补跑单独记录。
- v57完整评分36/40（90%），跨会话14/16、知识更新8/8、时间推理6/8、用户事实3/3、助手事实2/2、偏好3/3。相对v55修复3题、回退2题：完整周被误当成精确天数，名词后数字的累计编号未获解析。原嵌入服务停止造成18条失败；恢复同版本同摘要后补跑，先前22个答案逐字保留，40题缓存命中和Boolean评分完整。
- v58针对v57完整周结果被复核当成精确天数、改成错误单位后拒答的问题，保留程序计算的天数、完整周数和余数，并仅给来源及数值已通过的裸周数补完整周限定。实质文本、错误值、错误来源及只要数字的格式不自动改写；事件语义复核仍保留。440项测试通过，自拟检查通过，实际开发题1/1评分通过。整批已启动10题后，v57完整审计确认另一处编号解析错误，于是停止并合并修复；没有v58整批分数。
- v59增加显式累计进度声明下的名词后整数/英文编号解析，支持求和与字面分组计数；来源、单位、重复及算术检查不变，排名、偏好和计划仍需语义复核拒绝。新增检查覆盖编号、数值子串、小数、累计声明和单位混用，446项测试通过。先复查已观察的跨会话合计错误，再决定整批验证。评估启动脚本检查本地嵌入端口，避免服务停止时继续发出云端查询。
- v59实际合计题给出约27集，来源、算术与语义复核通过，自动评分却为错误；原始No标签保留并单列评分疑点。工具调用未检查断言命令的退出码便继续启动整批，发现后立即停止，保留4个部分答案，不报告整批分数。
- v60让跨对象合计先给总数，再简短列出各对象贡献，保留来源的估计限定，并同步编号规则说明。提示词长度触发一次小预算回归后压缩至2549字节，保留原失败日志；重建、446项测试及泄漏检查通过。新开发答案给出总数和两个分项，1/1自动评分通过；不重评或改写v59的原负标签。启动整批前单独确认成功退出码与已完成的正标签。
- v60完整评分36/40（90%），跨会话14/16、知识更新7/8、时间推理7/8。相对v57修复播客累计数和完整周数，回退午餐总数和电影数量。午餐凭据计算8通过，但答案把所有餐数都称为午餐；电影的5部被写成一个去重类别名，两次后续修正又遇到上游429。原始答案与标签保留，未启动另外40题。
- v61明确报告数量须用求和或分组大小，不能把类别名称当成多个命名实体；所有贡献使用有证据的共同单位，分项沿用来源名称，不添加未证实的子集限定。保持来源、去重、算术和语义复核，规则压缩至2538字节；先重建和检查，再复查两道已观察的回退题。
- v61开发复查1/2：电影的最新5部通过来源、算术、语义及自动评分；午餐修正仍使用sum并把来源名词写成不同item.unit，校验拒绝，整批未启动。规则压缩时遗漏CALCULATION冒号的初次测试失败记录保留，恢复标记后446项全部通过。
- v62在初次规则和数量修正指令中明确所有item.unit等于外层unit，来源名词放入各自quantity_label；数量限定也须适用于总数，不能从问题措辞直接继承。对混用单位也提供分组提示，钱款和时间仍禁止使用分组计数。规则2521字节；仅补查尚失败的开发午餐题，再决定完整40题。
- v62午餐开发补查1/1通过后，完整同题对照达到38/40（95%）：跨会话15/16、知识更新8/8、时间推理7/8，其他题型全部通过。相对v60修复2题、没有回退；全部40题缓存命中、BGE重排和Boolean评分完整，数字标签冲突0题。两个剩余负标签涉及未明确取得的唱片和未记录的取消日期，原始标签保留。
- v62达到同题门槛后，按此前登记的seed261011和固定题型配额，从v40剩余60条记录中抽取另外40题。核对题号零重叠、抽样与预登记一致、源码及配置与已通过版本完全相同；所有记录已有历史答案，因此这是无重叠诊断而非盲测或全量500题成绩。独立批次未完成前不采纳默认配置，也不宣称整体目标完成。
- v62无重叠批次完整评分35/40（87.5%）：跨会话13/16、知识更新8/8、时间推理7/8、用户事实2/3，助手事实及偏好全部通过。40题缓存、BGE重排及Boolean评分完整，数字标签冲突0题。第一组95%未在另一组保持，目标仍未完成，默认配置未采用。
- v63针对第二组已观察错误，允许同一单位内混合字面分组数量与值为1的命名个体，并对身份去重；新增实际出席日的字面日期集合计数和分页读取，不把占用的日期当成连续24小时。保留所有来源、单位、算术与语义核对，并保持月年报告的原始精度，数量回答简短列出已知对象。重建、461项测试及泄漏检查通过，先复查4个开发错题。车辆反馈的发生日及计划测试的实际完成日未被来源确认，不为匹配参考答案编造日期。第二组此后用于修复验证，后续分数明确作为固定开发样本对照，不能再称为新独立验证。
- v63开发复查3/4：鱼缸、出席日期和相机精度通过；婚礼数量及身份仍评分为错误。初次4/4启动断言失败，整批没有被连带启动。之后检查全部原文，发现婚礼参考中的一个配偶名字没有任何来源出现，保存source-review并保留原负标签。三类实际来源修复已通过，因此单独启动固定40题量化修复与回退；不重评同一答案、不补写缺失名字。鱼缸这次走状态读取，仅通过来源与语义核对；日期集合则实际通过程序数值校验。
- v64针对v63冻结评估中出现的一月记录十二月活动回退，新增recorded_at_past锚点：原始用户报告近期已完成事件、无明示年份时，可按该来源记录日解析最近的不晚于记录日的日期列表。锚点只接受规定字面字符串，事件措辞须为连续原话；助手内容、派生事实、显式其他年份及计划不能靠此锚点建立已参加事实，实际完成与近期语义仍需复核。出席日的完整原文、分页和直接读取均执行所需计算，避免latest_state计划跳过校验。重建、463项测试和泄漏检查通过；先单题复查，再决定必要40题，v63仍使用其冻结源码完成评分。
- v63完整评分35/40：跨会话14/16、知识更新7/8、时间推理7/8、用户事实3/3、助手事实2/2、偏好2/3。相对v62第二组修复出席日期、混合鱼数和月数精度，回退跨年活动、地点评分和电池建议，净增0题。所有40题缓存及BGE重排完整，Boolean评分完整、数字标签冲突0题。吉他答案与v62逐字相同却得到相反标签，另存评分波动审计；原始标签均保留，不重评相同答案。电池建议确实漏读了原文已有充电宝的拥有状态。
- v64跨年开发题给出3天，并计算三个2023年十二月日期，来源、去重、算术、实际参加复核及自动评分全部通过，1/1。没有启动v64整批；合并另外两类读取修复后再验证，减少不必要的40题运行。
- v65在原话索引及建议复核中按对象分别核对拥有状态：描述使用或整理自己的新物品可支持已拥有，另一对象的询问或计划不会抹掉该拥有状态；建议先沿用已有用品。完成动作的地点回忆先用完成报告的原始地点描述，早期考虑的商家名称须有身份关联。索引仍逐字保留角色、来源和计划措辞，超预算时仍省略索引。重建、463项测试及泄漏检查通过，先复查两道剩余回退。
- v65开发复查2/2通过：电池建议正确识别已有充电宝并提出沿用办法，地点回答先给完成原文中的音乐店位置。与v64的跨年1/1合并后，单独确认成功退出码和完整正标签，再启动必要固定40题；开发复查不计作整批分数，也不改写先前负标签。
- v65完整回答40题、全部缓存命中后，初次评分发生6条上游HTTP429端点不可用错误。已完成34条Boolean评分另存原始快照，只补评尚未评分的6题，未完成前不报整批分数。回答过程中观察到两处凭据格式失败：source_value误填时间短语，日期端点将来源中的今天改写成不在引用内的ISO日期；原拒答保留。
- v66让转换错误明确指出source_value字段，并说明数值、单位及原始时间短语的不同位置；同单位时省略多余转换字段。相对日期说明要求使用来源原话的relative_text与记录日锚点，省略伪造date_text；错误反馈也指出这种合法修正。来源、算术、单位及日期校验均保持。新检查验证错误字段定位及相对日期修正，同时保持非法数值被拒绝。初次小预算测试失败记录保留，压缩日期示例后重建、465项测试与泄漏检查全部通过；先开发复查再决定整批。
- v65补评完成，34条旧评分逐条与快照相同，最终35/40：跨会话14/16、知识更新8/8、时间推理5/8，其他题型全部通过。相对v63修复跨年活动、地点评分和已拥有用品的建议，回退时长合计格式、相对日期格式及上周六同行者，净增0题。同行者题已完成三页来源读取，最后调用遭遇HTTP429端点不可用，属于服务失败；原拒答和负标签保留。数字标签冲突0题，40条Boolean结果及缓存/BGE均完整。
- v67合并v66字段修复，并给每次respond的读取模型单独包一层：仅对HTTP429且明确server_error/端点不可用的失败调用，等待30秒后最多追加一次同请求重试。成功输出、正常拒答、额度/鉴权及内容错误不重采样；共享模型和写入器不被替换，附加重试及错误写入trace。降低整批并发6→4以减少端点失败峰值，模型角色、配置和记忆库仍相同，所以评估包含代码与执行并发调整，不能单独归因于一个改动。重建、470项测试及泄漏检查通过；先复查v65的3个回退。v66在整批和开发复查启动前合并，未产生分数。
- v67开发复查3/3通过：时长3.5周与阅读21天通过来源数值校验，同行者正确恢复为父母，原始评分全部为正。这次实际调用没有触发额外重试；策略由5项专门检查覆盖，不能把调用正常单独归因于该策略。单独确认完成退出码、缓存/BGE及Boolean正标签后，启动4并发固定40题。
- v67完整40题为34/40（85%），跨会话13/16、更新7/8、时间6/8，其他类型全对；相对v65修复时长3.5周、阅读21天，但竞争性运动计数、烘焙次数、支持小组次数因持续接口故障回退。四道可回答错题包含最终接口故障，婚礼参考名称缺失与悬挂反馈/实际测试日期缺失仍为负标签；另有一条未答题故障经过两次续跑后完成，原39回答不变。评分先完成31题，之后只补9个未评分题，再补最后1题；所有已完成Boolean评分逐项不变。缓存40/BGE40/Boolean40、数字标签异常0，两条历史读取错误与嵌套接口故障均保留，不因剔除接口失败而调整完整分母。没有达到95%。
- v67整批中实际出现多条延迟重试后仍失败的429，以及一条明确相同端点不可用消息的451；另有1题未产出答案，保留39个已完成回答，只续跑该未答题。v68依据这些已观察轨迹把每次失败读取的追加尝试上限改为3次，等待30/60/60秒；仅处理429或451且明确端点不可用，其他额度/鉴权/法律错误立即传播，成功回答或拒答不重采样。重建、473项测试与泄漏检查通过，固定同一模型、配置及写入库，并发再降到2；这包含执行并发调整，不能单独归因于代码。准备4个代表修复/校验/回答阶段的真实故障用例复查，后续发现的故障仍完整留在40题成绩中；前一冻结评测结束前不启动新的云端调用。报告新增嵌套的修复/校验接口故障统计，原始答案与评分仍计入完整分母。
- v68开发复查4/4通过，竞技运动2项、烘焙4次、支持小组5次均恢复，原本无来源的居住题正常完成校验后拒答。缓存/BGE/来源校验均为4/4，实际触发9次读取附加重试，单次失败调用最高用到第3次追加尝试。初次评分2题失败，只对缺失标签使用同一冻结退避包装器、原固定评分模型与官方提示词补评；已完成2个Boolean标签原样保留，补评实际触发1次附加重试。辅助驱动器首次导入路径错误发生在云调用前，日志保留，修正后完成。完整退出与4个正标签单独确认后才启动固定40题；4题结果不替代整批成绩。
- v68第二组固定开发样本完整39/40（97.5%）：跨会话16/16、更新8/8、时间7/8，其余全对；相对v67恢复四个接口故障错题，婚礼评分也转正，无回退。婚礼仍只回答来源支持的3场及新娘信息，没有补写缺失的参考名字；相关读取逻辑未改，原始生成与评分波动单独记录，不能把全部5个增益归因于退避策略。40题读取0接口错误，缓存/BGE/Boolean均40，数字异常0、未恢复读取接口失败0。评分首批28题保留原样，仅对12个缺失标签使用冻结退避、同一评分模型及官方提示词补评，实际追加3次重试。第一组固定回归已按相同源码/配置/2并发启动；两组均已用于开发，不是新独立或盲测。第一组完成且达标前不采用配置、不宣布整体目标完成。
- v68第一组完整复测33/40（82.5%）：跨会话12/16、更新7/8、时间7/8、偏好2/3，其余全对；相对v62第一组38/40的五个回退均有最终端点故障。完整审计有110次附加读取重试，7个仍未恢复的嵌套接口失败；40题缓存/BGE/Boolean完整，数字异常0。首次36道回答保留原样，只补4道未答题；首批19个标签保留，只补21个缺失评分，实际再重试7次。原先两道负标签及全部接口拒答均保留，两个固定组同版39/40与33/40不能证明整体稳定达到95%，因此不采用v68。
- v68第一组实际发现端点退避耗尽后，计算修复、来源复核或分页读取将接口故障吞为记忆不足的已完成答案。v69只让明确429/451端点不可用从这六处读取捕获路径继续抛出，交给现有评测续跑识别未完成题；不重新生成已完成答案，不改正常拒答、凭据错误、账户错误或任何评分标签，模型和有界退避保持相同。重建、477项测试及完整题库泄漏检查通过，写入/索引指纹保持不变。两组固定40题及两道实际故障开发复查均预先冻结，执行并发2→1也记录为对照条件变化；v68全组完成并保留原评分后，先启动小复查，通过后才决定整批。
- v47是错题审计确认后启动的必要修复验证：仍是同一40题、同一v40记忆与32KB/64KB预算，修正日期结果格式、程序复制日期来源和明确时长求和；另用2个自拟案例检查实际写入和回答。相对v40仍包含此前v42读取提示词变化，不能单独归因于一个修复。
- 用户进一步要求后续只抽约40题，未启动的v44整批100题已停止。v46–v48均使用按题型固定抽取的同一40题，未查看正确率或答案选择题目。v46与v40同时改变源码和预算，不能把分数差异归因于一个改动，也不能将40题结果当作100题结果。
- v38直接提供全量历史，跳过抽取、检索及证据复核，89/100仅用于诊断模型和数据的限制，不是记忆系统准确率。
- 同一模型用于回答及评分；评分标签不是人工裁定。精确字符串机制检查另行报告，不能替代题库评分。
- UTF-8预算按字节计数，配置字段名称中的tokens不能解释为实际模型token；实际用量查看usage账本。价格未配置时费用未知，不能将账本中的零当作实际零费用。
- 嵌入服务中断产生的失败日志保留；同一冻结版本只补跑未完成题，完整分母保持冻结样本的100题或40题。

## 改动与来源

跨页证据保留原始操作数和精确引文；说话人核对避免把助手的通用估价当成用户事实；历史状态辨认变更前一版本；完整原文在来源覆盖及预算都允许时直接合成；裁决输出截断保留新旧事实而不接受部分更新。

下列来源及预算用于复现，完整逐题审计位于各运行目录的answers-and-audit.json。

### accuracy-v21

源码：`87d6e312277aa613b1e05d5d672e6c98ebd582bca2d95252621657c2b5270587`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`7c925efafdddd55cf1e7993de095e62cc1be1ffb21ac25518beeda08074d72ff`；样本：`37be4482cff9dfcf85f2ab2502ce0c336db10a288b4e4cc90a7fef50a7920328`。
角色：`{"chat": "opencode-gpt-6-luna", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-gpt-6-luna"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 3500, "answer_input_budget_tokens": 8192, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"qa": 59, "paged_evidence": 41}`；历史调用失败记录：0，仍未答出的失败题：0。

真实端到端机制检查：`{"total": 12, "passed": 12, "incorrect": 0, "errors": 0, "unstarted": 0}`。

配对：14→14；新增正确2题，回退2题。

### accuracy-v22

源码：`e06849e8ab0a46da57caa0b8d782ba1b3877eb8b4dc98489016229e1f6a67b6d`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`7c925efafdddd55cf1e7993de095e62cc1be1ffb21ac25518beeda08074d72ff`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-gpt-6-luna", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-gpt-6-luna"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 3500, "answer_input_budget_tokens": 8192, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"qa": 61, "paged_evidence": 39}`；历史调用失败记录：1，仍未答出的失败题：0。

配对：53→55；新增正确17题，回退15题。

### accuracy-v23

源码：`b9e4e224b0669cad28c6a28546b4883a837db0587c655eb38b960d651e997adc`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`7c925efafdddd55cf1e7993de095e62cc1be1ffb21ac25518beeda08074d72ff`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-gpt-6-luna", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-gpt-6-luna"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 3500, "answer_input_budget_tokens": 8192, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"qa": 56, "paged_evidence": 44}`；历史调用失败记录：0，仍未答出的失败题：0。

真实端到端机制检查：`{"total": 19, "passed": 17, "incorrect": 2, "errors": 0, "unstarted": 0}`。

配对：55→61；新增正确14题，回退8题。

### accuracy-v32-longcat-reader

源码：`b7c0639024509e0c1bce016b1ab2d3df103927aeb264353cdf24d3d96c080d68`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`3bbeb257834e2e53800bd2e00cd818fd6085ad91b5a9883706f238f3891a414b`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-longcat-2.5-preview-free", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-longcat-2.5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 3500, "answer_input_budget_tokens": 8192, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"paged_evidence": 96, "qa": 4}`；历史调用失败记录：15，仍未答出的失败题：0。

配对：62→70；新增正确15题，回退7题。

### accuracy-v34-step-context

源码：`b7c0639024509e0c1bce016b1ab2d3df103927aeb264353cdf24d3d96c080d68`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`5d6a447d7032e3175da39057b21e6ffa24b7c7acc897a5edc1a0f8af3b70b427`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 8000, "answer_input_budget_tokens": 16384, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"paged_evidence": 94, "qa": 6}`；历史调用失败记录：0，仍未答出的失败题：0。

配对：62→78；新增正确20题，回退4题。

### accuracy-v35-bounded-selection

源码：`ab3614c7b2b78222baa389ea81aaa1aff1e3e14ac7176af9d192a1e5272ec61f`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`5d6a447d7032e3175da39057b21e6ffa24b7c7acc897a5edc1a0f8af3b70b427`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 8000, "answer_input_budget_tokens": 16384, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"paged_evidence": 94, "qa": 6}`；历史调用失败记录：25，仍未答出的失败题：0。

配对：78→72；新增正确2题，回退8题。

### accuracy-v36-speaker-and-difference

源码：`0e5149d1ca4348cdad5f7f0ac8fb9f3c301452c1e53e3aa39cde50e5f2a05e72`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`5d6a447d7032e3175da39057b21e6ffa24b7c7acc897a5edc1a0f8af3b70b427`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 8000, "answer_input_budget_tokens": 16384, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"paged_evidence": 92, "qa": 8}`；历史调用失败记录：0，仍未答出的失败题：0。

真实端到端机制检查：`{"total": 22, "passed": 21, "incorrect": 0, "errors": 1, "unstarted": 0}`。

配对：78→82；新增正确5题，回退1题。

### accuracy-v37-larger-context

源码：`0e5149d1ca4348cdad5f7f0ac8fb9f3c301452c1e53e3aa39cde50e5f2a05e72`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`b10d36bde598cbcc2d0ecc1ab9d66c2968dff554a1a0ca3a63feac9b04bc8219`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"paged_evidence": 95, "qa": 5}`；历史调用失败记录：1，仍未答出的失败题：0。

配对：82→75；新增正确2题，回退9题。

### accuracy-v38-full-context-reference

源码：`0e5149d1ca4348cdad5f7f0ac8fb9f3c301452c1e53e3aa39cde50e5f2a05e72`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`b10d36bde598cbcc2d0ecc1ab9d66c2968dff554a1a0ca3a63feac9b04bc8219`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": null}`。
读取路径：`{"full-context": 100}`；历史调用失败记录：0，仍未答出的失败题：0。

### accuracy-v39-complete-context

源码：`38b1fdb03ff6b72782d39c59ba567f9774906349df0d0baf9156bbe00e8bfd22`；写入源码：`bf6d3b869ed651811adfac188d152aaa20634e301c0711cd26c555e4186fb9b7`。
配置：`763513a06c1d7b1959389f1a4d0efac98e7c8a38d377af4cf202cfc2635f6daf`；样本：`542b27caa588e5dc9c665e975cdf9e5a23051306e5f2e2638c3c78cf20b0c9d6`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-gpt-6-luna", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 75, "paged_evidence": 24, "qa": 1}`；历史调用失败记录：1，仍未答出的失败题：0。

配对：75→79；新增正确6题，回退2题。

### accuracy-v40-fresh-complete-context

源码：`ffaa1610fce86ab08e36417f69c31e661e8c3a6bd045da60751e506b2a7de76a`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`2a47c45f4afadc779dfee01f2e21bf64e13403e0681af771e3c2d34a42ec2548`；样本：`2485ad445af49ddf0b7816abf926e493b27b8a8dbae6fb6a98eb63b9cd2b1cea`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 68, "paged_evidence": 30, "qa": 2}`；历史调用失败记录：0，仍未答出的失败题：0。

### accuracy-v41-full-evidence-budget

源码：`ffaa1610fce86ab08e36417f69c31e661e8c3a6bd045da60751e506b2a7de76a`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`55c3ade6311c2b276f6ef0654433443c679b57669c8ecc78e5849bdf3d5f38af`；样本：`2485ad445af49ddf0b7816abf926e493b27b8a8dbae6fb6a98eb63b9cd2b1cea`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 96000, "answer_input_budget_tokens": 131072, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

真实端到端机制检查：`{"total": 7, "passed": 7, "incorrect": 0, "errors": 0, "unstarted": 0}`。

执行状态：`cancelled_before_benchmark`。

### accuracy-v42-qualified-recall

源码：`c729605b531fa4dadf0adc906ffc9b57945294138b99e3a6b31da5b8e3808bd6`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`55c3ade6311c2b276f6ef0654433443c679b57669c8ecc78e5849bdf3d5f38af`；样本：`2485ad445af49ddf0b7816abf926e493b27b8a8dbae6fb6a98eb63b9cd2b1cea`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 96000, "answer_input_budget_tokens": 131072, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

执行状态：`cancelled_before_benchmark`。

### accuracy-v43-low-read-reasoning

源码：`c729605b531fa4dadf0adc906ffc9b57945294138b99e3a6b31da5b8e3808bd6`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`20de6d860f1f6fb91097ef4e595c5bc1b057f29d69ff4fb3a80e4fdc5e78ba40`；样本：`2485ad445af49ddf0b7816abf926e493b27b8a8dbae6fb6a98eb63b9cd2b1cea`。
角色：`{"chat": "opencode-step-5-preview-free-low", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-low"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 96000, "answer_input_budget_tokens": 131072, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

执行状态：`cancelled_before_benchmark`。

### accuracy-v44-selective

源码：`c729605b531fa4dadf0adc906ffc9b57945294138b99e3a6b31da5b8e3808bd6`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`88dbfd5a4e563b4203cb9dbb0b6dcb08207e5525398507f2152d0f5476926a44`；样本：`2485ad445af49ddf0b7816abf926e493b27b8a8dbae6fb6a98eb63b9cd2b1cea`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 8000, "answer_input_budget_tokens": 16384, "memory_input_budget_tokens": 16384, "complete_episode_context": false}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

执行状态：`cancelled_before_benchmark`。

### accuracy-v45-selective-low

源码：`c729605b531fa4dadf0adc906ffc9b57945294138b99e3a6b31da5b8e3808bd6`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`6be920d588c726cac085d7873248fb903d92bf9b3b9b3dc2d1fc09143020cb0a`；样本：`2485ad445af49ddf0b7816abf926e493b27b8a8dbae6fb6a98eb63b9cd2b1cea`。
角色：`{"chat": "opencode-step-5-preview-free-low", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-low"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 8000, "answer_input_budget_tokens": 16384, "memory_input_budget_tokens": 16384, "complete_episode_context": false}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

执行状态：`cancelled_before_benchmark`。

### accuracy-v46-necessary40

源码：`c729605b531fa4dadf0adc906ffc9b57945294138b99e3a6b31da5b8e3808bd6`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`88dbfd5a4e563b4203cb9dbb0b6dcb08207e5525398507f2152d0f5476926a44`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 8000, "answer_input_budget_tokens": 16384, "memory_input_budget_tokens": 16384, "complete_episode_context": false}`。
读取路径：`{"paged_evidence": 38, "qa": 2}`；历史调用失败记录：0，仍未答出的失败题：0。

真实端到端机制检查：`{"total": 8, "passed": 8, "incorrect": 0, "errors": 0, "unstarted": 0}`。

配对：30→30；新增正确1题，回退1题。

### accuracy-v47-calculation-contract40

源码：`36b787d135d713e2e38ae9fc734fb6d17de95f10bf4963147665160d1148af32`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`2a47c45f4afadc779dfee01f2e21bf64e13403e0681af771e3c2d34a42ec2548`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 29, "paged_evidence": 11}`；历史调用失败记录：0，仍未答出的失败题：0。

真实端到端机制检查：`{"total": 2, "passed": 2, "incorrect": 0, "errors": 0, "unstarted": 0}`。

配对：30→31；新增正确2题，回退1题。

### accuracy-v48-category-evidence40

源码：`cfd54e987f69df63d272bcd9905b205d0cafbb8a0b2a1faf5173b494ede54e6f`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`2a47c45f4afadc779dfee01f2e21bf64e13403e0681af771e3c2d34a42ec2548`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 29, "paged_evidence": 11}`；历史调用失败记录：0，仍未答出的失败题：0。

真实端到端机制检查：`{"total": 1, "passed": 1, "incorrect": 0, "errors": 0, "unstarted": 0}`。

配对：31→30；新增正确1题，回退2题。

### accuracy-v49-grounded-quantities40

源码：`ce3772424526f56713720077747c9c20b352bf896181e014ba7b88666b538260`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`2a47c45f4afadc779dfee01f2e21bf64e13403e0681af771e3c2d34a42ec2548`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 29, "paged_evidence": 11}`；历史调用失败记录：0，仍未答出的失败题：0。

真实端到端机制检查：`{"total": 4, "passed": 2, "incorrect": 2, "errors": 0, "unstarted": 0}`。

执行状态：`evaluated`。

配对：31→33；新增正确3题，回退1题。

### accuracy-v50-bounded-recovery40

源码：`c902cfc42785655a7007d57b47afe295b2777ff7b21428a2fc9217a13d7c19d6`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`3a80da2f2bd588bf4c1dcf2350c35cc17677e1e6a052721768d1296e8c26652a`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-low", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-low"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

自拟真实模型读取检查（不含写入，不是题库分数）：`{"mode": "self-authored live reader only; no memory writes; not a benchmark score", "total": 6, "passed": 5}`。

执行状态：`cancelled_before_benchmark`。

### accuracy-v51-literal-counts40

源码：`137ff0c9e76b71f23193cb196fe299d92525bc4bb48cae4d3b0130338b2cedfd`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`3a80da2f2bd588bf4c1dcf2350c35cc17677e1e6a052721768d1296e8c26652a`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-low", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-low"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 28, "paged_evidence": 11, "qa": 1}`；历史调用失败记录：0，仍未答出的失败题：0。

自拟真实模型读取检查（不含写入，不是题库分数）：`{"mode": "self-authored live reader only; no memory writes; not a benchmark score", "total": 2, "passed": 2}`。

执行状态：`evaluated`。

配对：33→34；新增正确5题，回退4题。

### accuracy-v52-calendar-precision40

源码：`2be66dd685fe25b1e1fbc45381bef776eda9dafb8616d15d7ab11c195b3a297e`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

自拟真实模型读取检查（不含写入，不是题库分数）：`{"mode": "live reader checks; two self-authored arithmetic cases and two previous-context advice replays; no ingestion or retrieval score", "total": 1, "passed": 1, "call_errors": 1, "ungraded": 2}`。

执行状态：`cancelled_before_benchmark`。

### accuracy-v53-preference-context40

源码：`4ab4a800cc59e126ba7d864e99e819f878c4b4ddeaccd3a286f7a37bc2d2b927`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "four previously wrong development questions; no full-batch or holdout score", "answered": 4, "graded": 4, "correct": 3}`。

执行状态：`carried_forward_without_full_benchmark`。

### accuracy-v54-completed-event-bounds40

源码：`12dec25f676d4e3b15395cda97f243d7751a50d44f02e8888867797953ad7468`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

自拟真实模型读取检查（不含写入，不是题库分数）：`{"mode": "One self-authored live date-bound check; not a memory benchmark score.", "total": 1, "passed": 1, "call_errors": 0, "ungraded": 0}`。

开发子集复查（不是整批分数）：`{"mode": "one previously wrong temporal development case; no full-batch or holdout score", "answered": 1, "graded": 1, "correct": 0}`。

执行状态：`carried_forward_without_full_benchmark`。

### accuracy-v55-verified-date-format40

源码：`2c3566ef928c153d8ceceb454acc31ca85032170f9afc65b21d1bc8c56557b11`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 29, "paged_evidence": 11}`；历史调用失败记录：0，仍未答出的失败题：0。

自拟真实模型读取检查（不含写入，不是题库分数）：`{"mode": "Replay recorded date proof through presentation repair and live support review.; no new benchmark score", "total": 1, "passed": 1, "call_errors": 0, "ungraded": 0}`。

执行状态：`evaluated`。

配对：34→35；新增正确3题，回退2题。

### accuracy-v56-requested-bounds40

源码：`58cfd62fddd71c8696709e8e40d4b5f481dbbd1602a44455bb4182cd367a1008`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

自拟真实模型读取检查（不含写入，不是题库分数）：`{"mode": "One self-authored live date-bound check; not a memory benchmark score.", "total": 1, "passed": 0, "call_errors": 0, "ungraded": 0}`。

开发子集复查（不是整批分数）：`{"mode": "three previously wrong development questions; not a full or independent benchmark score", "answered": 3, "graded": 3, "correct": 3}`。

执行状态：`carried_forward_without_full_benchmark`。

### accuracy-v57-bound-schema40

源码：`6234abe4d6565e0546173b201c0efaaffad299ac953a7ba4a9c3c80148355eca`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 29, "paged_evidence": 11}`；历史调用失败记录：18，仍未答出的失败题：0。

自拟真实模型读取检查（不含写入，不是题库分数）：`{"mode": "One self-authored live date-bound check; not a memory benchmark score.", "total": 1, "passed": 0, "call_errors": 0, "ungraded": 0}`。

执行状态：`evaluated`。

配对：35→36；新增正确3题，回退2题。

### accuracy-v58-complete-weeks40

源码：`bb53d79af460fe7a0ba6ef6589cef9073db18de7d31d5ba8a4fa87d10683286b`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 7, "paged_evidence": 3}`；历史调用失败记录：0，仍未答出的失败题：0。

自拟真实模型读取检查（不含写入，不是题库分数）：`{"mode": "One self-authored complete-week check with a live answer and semantic review; no memory benchmark score.", "total": 1, "passed": 1, "call_errors": 0, "ungraded": 0}`。

开发子集复查（不是整批分数）：`{"mode": "Development subset of the observed complete-week regression; not a complete or independent40 score.", "answered": 1, "graded": 1, "correct": 1}`。

执行状态：`stopped_after_new_aggregate_regression_discovered`。

### accuracy-v59-cumulative-index40

源码：`cf58675236af913a7475f6a7aa5fc0e96de0f2f9d617b57fe5fe73fb84c40ed2`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 4}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Development subset of the observed cumulative-index regression; not a complete or independent40 score.", "answered": 1, "graded": 1, "correct": 0}`。

执行状态：`stopped_after_development_judge_failure`。

### accuracy-v60-aggregate-breakdown40

源码：`a8a4746f89ce912d68284eaf9ebbf80946ed0881b5e5e35edf8eed5d1967eff6`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 29, "paged_evidence": 11}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Development subset; not a complete or independent40 score.", "answered": 1, "graded": 1, "correct": 1}`。

执行状态：`evaluated`。

配对：36→36；新增正确2题，回退2题。

### accuracy-v61-count-scope40

源码：`d58f8e265952e688d00fad59bd5264870af87a51ed41b12ed0b5f19162ad432a`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Development subset; not a complete or independent40 score.", "answered": 2, "graded": 2, "correct": 1}`。

执行状态：`stopped_before_full_benchmark_after_development_failure`。

### accuracy-v62-independent40

源码：`4692e64a8636a2b058bfff0f7ca1e6c2cef9a33ccb44b8198a6bd24a23b1f886`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`eec12c6cc522034d942ed18f51db306b46f7698a71df8b2e5142216f0df24fea`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 26, "paged_evidence": 12, "qa": 2}`；历史调用失败记录：0，仍未答出的失败题：0。

执行状态：`evaluated_target_not_met`。

### accuracy-v62-literal-group-units40

源码：`4692e64a8636a2b058bfff0f7ca1e6c2cef9a33ccb44b8198a6bd24a23b1f886`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 29, "paged_evidence": 11}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Development subset only; not a complete or independent40 score.", "answered": 1, "graded": 1, "correct": 1}`。

执行状态：`paired_passed_separate_sample_failed`。

配对：36→38；新增正确2题，回退0题。

### accuracy-v63-mixed-evidence40

源码：`f60c0cf5087d410cab8c8b361b777207bbfd2898237547ebf18d992f29328fb2`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`f27a126a9bd8286a5d7722673dc33a36c26475eb3e405f8d415447caae04a218`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 26, "paged_evidence": 13, "qa": 1}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Development subset only; not a complete, independent or blind40 score.", "answered": 4, "graded": 4, "correct": 3}`。

执行状态：`evaluated_with_attendance_year_regression`。

配对：35→35；新增正确3题，回退3题。

### accuracy-v64-attendance-year40

源码：`1a962d075deb4908640ef375eceb4084251ab247bf049d2635db9c12a019a069`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`f27a126a9bd8286a5d7722673dc33a36c26475eb3e405f8d415447caae04a218`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Observed development regression only; not a complete or independent40 score.", "answered": 1, "graded": 1, "correct": 1}`。

执行状态：`native_passed_merged_into_v65_before_benchmark`。

### accuracy-v65-possession-continuity40

源码：`322b288a26fc8f0f927b902e0e31235d97c2a5ef2bff00aca1ecdfd92714cea1`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`f27a126a9bd8286a5d7722673dc33a36c26475eb3e405f8d415447caae04a218`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 25, "paged_evidence": 13, "qa": 2}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Observed development subset only; not a complete or independent40 score.", "answered": 2, "graded": 2, "correct": 2}`。

执行状态：`evaluated_target_not_met`。

配对：35→35；新增正确3题，回退3题。

### accuracy-v66-field-specific-repair40

源码：`2641f508e800cae7fb3fb7e36aa7ada2f849914b29a12c033b8d6a601fa07d1b`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`f27a126a9bd8286a5d7722673dc33a36c26475eb3e405f8d415447caae04a218`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

执行状态：`cancelled_before_benchmark`。

### accuracy-v67-read-transport-recovery40

源码：`cd88e3dff03cc6ca04d012b681034f255495a3a76a69f5a1c7e2c45dc3a32096`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`f27a126a9bd8286a5d7722673dc33a36c26475eb3e405f8d415447caae04a218`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"qa": 3, "paged_evidence": 12, "complete_original_context": 25}`；历史调用失败记录：2，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Observed development subset only; not a complete or independent40 score.", "answered": 3, "graded": 3, "correct": 3}`。

执行状态：`evaluated_target_not_met`。

配对：35→34；新增正确2题，回退3题。

### accuracy-v68-bounded-endpoint-backoff40

源码：`dd3de67a72681706e80a511f3a665054c300a2635e9e49844b68d75cb9a49e15`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`f27a126a9bd8286a5d7722673dc33a36c26475eb3e405f8d415447caae04a218`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 25, "paged_evidence": 11, "qa": 4}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": "Observed development subset only; not a complete or independent40 score.", "answered": 4, "graded": 4, "correct": 4}`。

执行状态：`second_group_passed_same_source_first_group_failed`。

配对：34→39；新增正确5题，回退0题。

### accuracy-v68-regression40

源码：`dd3de67a72681706e80a511f3a665054c300a2635e9e49844b68d75cb9a49e15`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{"complete_original_context": 27, "paged_evidence": 11, "qa": 2}`；历史调用失败记录：4，仍未答出的失败题：0。

执行状态：`evaluated_target_not_met`。

配对：38→33；新增正确0题，回退5题。

### accuracy-v69-propagate-read-transport40

源码：`fb8a20f62ae08e24f6a5b6b3fa6e4a1b58b3e11e7b4ed5fd54ff66e19b0b234a`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`c14a7c4501a1cfd73b72331802e82c3616c01348d685f70955a73265429a7ff1`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

开发子集复查（不是整批分数）：`{"mode": null, "answered": 0, "graded": 0, "correct": 0}`。

执行状态：`native_development_check_running`。

### accuracy-v69-second-regression40

源码：`fb8a20f62ae08e24f6a5b6b3fa6e4a1b58b3e11e7b4ed5fd54ff66e19b0b234a`；写入源码：`72c120e94379849f019e4b6b3d7fe12920c638ed6c383aa0aecdc305e7b1a167`。
配置：`7494f4886cb5b1bd4d7677d66830d47b7e112793a86c669cdddf13fc9c6a090f`；样本：`f27a126a9bd8286a5d7722673dc33a36c26475eb3e405f8d415447caae04a218`。
角色：`{"chat": "opencode-step-5-preview-free-reader", "memory": "opencode-longcat-2.5-preview-free", "embedding": "bge-m3", "judge": "opencode-step-5-preview-free", "query": "opencode-step-5-preview-free-reader"}`。
预算：`{"budget_mode": "utf8", "context_budget_tokens": 32000, "answer_input_budget_tokens": 65536, "memory_input_budget_tokens": 16384, "complete_episode_context": true}`。
读取路径：`{}`；历史调用失败记录：0，仍未答出的失败题：0。

执行状态：`frozen_waiting_first_group_gate_no_cloud_calls`。

