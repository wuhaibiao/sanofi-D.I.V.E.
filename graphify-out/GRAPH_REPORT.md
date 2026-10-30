# Graph Report - src  (2026-10-10)

## Corpus Check
- 47 files · ~51,708 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 978 nodes · 2794 edges · 42 communities (41 shown, 1 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 191 edges (avg confidence: 0.94)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Paged Numeric Evidence
- HTTP Protocol Streaming
- Core Functions
- Benchmark Provenance Methods
- Write Functions
- Retrieval Scale Measures
- HTTP Protocol Streaming
- Memory Source Storage
- Transport Protocol Clients
- Benchmark Cache Scales
- Llm Functions
- Persisted Memory Schema
- Model Discovery Fallbacks
- CLI Benchmark Commands
- Memory Settings
- Persisted Memory Schema
- Fake Functions
- Mcp Functions
- Parallel Evaluation Files
- Multilingual Keyword Processing
- Memory Source Storage
- Database Query Results
- Judge Functions
- Memory Source Storage
- Agent Proxy Borrowing
- Update Functions
- Anthropic Client Functions
- Llm Functions
- Database Query Results
- Question Planning Coverage
- Leakcheck Functions
- Cli Functions
- Maintenance Functions
- Lifecycle Functions
- Memory Source Storage
- Llm Functions
- Opencode Go Functions
- Timeutil Functions
- Vector Math Backends
- Hybrid Memory Retrieval
- Judge Functions
-   Init   Functions

## God Nodes (most connected - your core abstractions)
1. `Store` - 99 edges
2. `MemorySystem` - 72 edges
3. `ModelError` - 65 edges
4. `MemorySettings` - 46 edges
5. `UsageLedger` - 45 edges
6. `Models` - 36 edges
7. `Fact` - 34 edges
8. `MemoryService` - 29 edges
9. `budget_counter()` - 28 edges
10. `ChatApp` - 28 edges

## Surprising Connections (you probably didn't know these)
- `aggregation_pages()` --uses--> `Fact`  [INFERRED]
  memsys/aggregate_batch.py → memsys/schema.py
- `paged_aggregate()` --uses--> `ModelError`  [INFERRED]
  memsys/aggregate_batch.py → memsys/llm.py
- `paged_aggregate()` --uses--> `Fact`  [INFERRED]
  memsys/aggregate_batch.py → memsys/schema.py
- `_semantic_finish()` --uses--> `ModelError`  [INFERRED]
  memsys/aggregation.py → memsys/llm.py
- `answer_with_validation()` --uses--> `ChatModel`  [INFERRED]
  memsys/aggregation.py → memsys/llm.py

## Import Cycles
- None detected.

## Communities (42 total, 1 thin omitted)

### Community 0 - "Paged Numeric Evidence"
Cohesion: 0.06
Nodes (81): decimal, functools, aggregation_pages(), derived_coverage(), materialize_item(), _page_parts(), paged_aggregate(), 聚合候选分批阅读：逐页核验来源与覆盖，然后用程序合并贡献。 (+73 more)

### Community 1 - "HTTP Protocol Streaming"
Cohesion: 0.06
Nodes (33): BaseHTTPRequestHandler, http_server, recall() 的结果。context 整段放进外部模型的系统提示词即可；memory / rules 分开给，方便自己排版。, Recall, AnthropicMessages, _Handler, Job, MemoryService (+25 more)

### Community 2 - "Core Functions"
Cohesion: 0.07
Nodes (34): MemorySystem, Any, datetime, 原文立刻写进日志；整理成记忆要等会话结束（或累计够轮数）。, 单独跑一次巩固与遗忘。会话结束时会自动跑；时间流逝本身（模拟时钟推进、隔天再打开）也该跑一次。, 只取记忆，不生成回答：给外部 agent 用。budget 是这次给记忆的 token 预算，不传用配置里的。, 读取路径本身：查询分析 → 检索 → 重排 → 预算选择 → 排成时间线。返回 (trace, 选中的记忆, 记忆块)。, 外部 agent 的聚合分页入口；每页有界，返回覆盖信息，不自动拼入单个提示词。 (+26 more)

### Community 3 - "Benchmark Provenance Methods"
Cohesion: 0.07
Nodes (43): ast, dataclasses, datetime, BehaviorCase, Any, 自拟的回答行为回归：固定记忆检查生成策略，不替代 LongMemEval 评测。, run_behavior(), GoldLabel (+35 more)

### Community 4 - "Write Functions"
Cohesion: 0.08
Nodes (40): 按时间顺序补抽降级或抽取不完整的轮次，成功后才移出队列。, 启动时重试持久补抽队列，再整理上次没正常关闭的会话。, 换了 embedding 模型后重建向量：原文和事实都在，随时可以重算。, ChunkInput, episode_index(), EpisodeIndex, 原文向量的无损分段计划；位置始终指向对应说话人的原文字符区间。, stored_chunks() (+32 more)

### Community 5 - "Retrieval Scale Measures"
Cohesion: 0.07
Nodes (26): calendar, ChatApp, _fact_line(), format_trace(), 处理一行输入；返回 False 表示退出。任何一条输入出错都不让程序退出。, 距离上次整理过了一段时间（重新打开程序、模拟时钟推进），跑一次遗忘策略。, 退出时结束会话。整理失败也照样退出：原文已在日志里，下次启动会自动补整理。, 参数可以是事实编号（F12），也可以是关键词：取最相关的那条事实，显示它的版本链。 (+18 more)

### Community 6 - "HTTP Protocol Streaming"
Cohesion: 0.07
Nodes (29): AppConfig, Any, 以下划线开头的键视为注释，方便在 JSON 里写说明。, 配置里写了的角色照配置来，没写的交给 discover 去找。, chat_model(), 给 Python 写的 agent 用的入口。 llm：记忆模型，传函数 fn(messages) → 文本，或任何有 chat()…, config_from_discovery(), discovered_configs() (+21 more)

### Community 7 - "Memory Source Storage"
Cohesion: 0.07
Nodes (10): 已经建过的会话的外部编号（例如 LongMemEval 的会话 id），用来避免重复载入。, 外部编号对应的、还开着的会话（例如 Claude Code 的会话 id）；没有就返回 None。, 只记录来源原文中唯一、逐字匹配的证据区间；不能凭空生成位置。, 明确 literal value + 唯一来源区间才精细处理；条件不足时由调用方保守屏蔽。, 同一主体、同一属性的当前事实，更新裁决时优先拿来比对。, 同一件事又被说了一次：提及次数加一，置信度换成调用方算好的新值（见 update.py），补上来源。, 可检索及归档原文的索引覆盖；遗忘和未进索引的寒暄不在分母中。, BM25 检索；返回 (id, 分数)，分数越大越相关。 (+2 more)

### Community 8 - "Transport Protocol Clients"
Cohesion: 0.12
Nodes (20): concurrent_futures, http_client, json, logging, math, Anthropic Messages API 的对话客户端（可选）：只有 key 是 ANTHROPIC_API_KEY、或者代理的上游是 Anthropic…, 评测输出用的 JSONL 读写。 进程可能在写到一半时被杀掉，留下一行残缺的记录。读的时候跳过坏行，写的时候先补上换行，…, 不联网的替身模型，用于测试和 --fake 演练。 - HashEmbedder：特征哈希的词袋向量，确定性、无依赖，词面相近的文本向量相近。 -… (+12 more)

### Community 9 - "Benchmark Cache Scales"
Cohesion: 0.11
Nodes (23): argparse, judge_offline(), 用 offline_label 给答案文件打分，输出格式与官方脚本相同；偏好题不写入（未评分）。, ConfigChanged, ValueError, cmd_doctor(), cmd_init(), _config_path() (+15 more)

### Community 10 - "Llm Functions"
Cohesion: 0.09
Nodes (14): embed_model(), FunctionEmbedder, ModelError, KeySource, RuntimeError, 把任意函数包成 EmbeddingModel：fn(texts) → 向量列表。, 调用模型服务失败：不可重试的 HTTP 错误、重试耗尽，或返回内容不是 JSON。, key 无效、余额不足、没有权限：再试多少次都一样，应该停下来让人处理。 (+6 more)

### Community 11 - "Persisted Memory Schema"
Cohesion: 0.11
Nodes (17): array, base64, contextlib, list, EpisodeChunk, _normalize(), ValueError, 记忆存储：每个用户（评测时是每道题）一个 SQLite 文件。 选 SQLite… (+9 more)

### Community 12 - "Model Discovery Fallbacks"
Cohesion: 0.11
Nodes (15): make_client(), 配置：模型（谁来做什么）和记忆策略的所有可调参数。 配置文件是 JSON，结构见仓库根目录的 config.example.json： {"models":…, 按 ModelConfig.api 建对话/embedding 客户端；本地哈希向量另见 discover.hash_embedder。, Message, QuotaFallbackChat, 额度不足或限流时按顺序切换，成功后继续用它；全被限流时等待冷却。, ChatModel, EmbeddingModel (+7 more)

### Community 13 - "CLI Benchmark Commands"
Cohesion: 0.21
Nodes (21): ArgumentParser, build_parser(), cmd_bench_behavior(), cmd_bench_judge(), cmd_bench_load(), cmd_bench_mechanisms(), cmd_bench_report(), cmd_bench_run() (+13 more)

### Community 14 - "Memory Settings"
Cohesion: 0.12
Nodes (6): _fake_models(), MemorySettings, Models, 配置文件写错（类型不对、比例超出 0~1、预算为负）时尽早报清楚，而不是跑到一半出怪结果。, 写入相关参数和提示词的指纹；评测时据此判断缓存的记忆库能不能复用。, data_dir 为空时记忆只在内存里（测试用）；否则每个用户一个 SQLite 文件。 wal=True…

### Community 15 - "Persisted Memory Schema"
Cohesion: 0.14
Nodes (12): hashlib, bounded_spans(), ModelTokenizer, pinned_revision(), 模型专用 token 计数；不改变回答上下文的 UTF-8/估算预算。, 连续、无损句段；代码围栏和列表条目优先保持结构。, 每个窗口以实际完整输入核验；区间保留所有空白与 Unicode 字符。, structure_spans() (+4 more)

### Community 16 - "Fake Functions"
Cohesion: 0.17
Nodes (10): HeuristicChat, Message, handlers: purpose → 固定字符串、按顺序消费的字符串列表，或接收 messages 的函数。, 按 purpose 用规则生成一个格式正确的回复。, _result(), ScriptedChat, ChatResult, FunctionChat (+2 more)

### Community 17 - "Mcp Functions"
Cohesion: 0.20
Nodes (8): McpServer, Any, datetime, TextIO, 向客户端发请求并等回复（sampling 用）。, 第一次调用工具时补整理上次没正常结束的会话（要等 initialized 之后 sampling 才能用）。, 客户端断开：结束当前会话。整理失败（例如 sampling 已经用不了）也没关系，下次启动会补。, build_system(sampling_model) 在 initialize 时调用：客户端支持 sampling 时传入借来的模型，否则传 None。

### Community 18 - "Parallel Evaluation Files"
Cohesion: 0.19
Nodes (15): glob, build_report(), _cost_cell(), find_eval_log(), rank(), _first_per_question(), _judge_model(), _known_cost() (+7 more)

### Community 19 - "Multilingual Keyword Processing"
Cohesion: 0.20
Nodes (14): build_store(), measure(), _quantile(), 检索延迟随记忆规模的变化（手册 B.2"效率"）。 LongMemEval Oracle 每题只有几十到几百条记忆，看不出规模效应。这里用合成数据把一个记忆库…, 灌入 items 条记忆：一半事实、一半原文轮次，每条事实指向一轮原文。, ScaleRow, _sentence(), to_markdown() (+6 more)

### Community 20 - "Memory Source Storage"
Cohesion: 0.20
Nodes (6): EmbeddingMismatch, RuntimeError, 库里的向量是用别的 embedding 模型算的，要先 reindex。, 第一次写入时记下 embedding 模型名；之后名字对不上就拒绝，避免新旧向量混在一起比较。, 换 embedding 模型：在一个事务里清空旧向量、写入新向量并改掉模型名，中途失败不留半截。, Remove retired retrieval tables only inside an explicit reindex/cleanup…

### Community 21 - "Database Query Results"
Cohesion: 0.12
Nodes (7): Connection, _LockedConnection, RLock, 同一个 sqlite3 连接被几个线程同时用会出错，Python 3.9 上甚至直接崩溃（MCP 并发的工具调用、…, wal=True 用于服务模式：同一个库有多个连接（请求线程读、后台线程整理），读写互不阻塞。, 别的连接提交过写入，这个数就会变（SQLite 的 data_version），用来让向量缓存失效。, Upgrade a pre-FTS legacy index from its existing safe source ranges.

### Community 22 - "Judge Functions"
Cohesion: 0.21
Nodes (15): append_jsonl(), Any, read_jsonl(), anscheck_prompt(), _grade(), _hypotheses(), judge(), one() (+7 more)

### Community 23 - "Memory Source Storage"
Cohesion: 0.19
Nodes (7): date, datetime, 事务，可以嵌套：只有最外层负责提交或回滚。 sqlite3 的 `with conn:` 不能嵌套——内层退出时就会把外层的半截事务提交掉，…, 在写入事务里调用：记下已整理到第几句；给了 closed_at 就同时关闭会话。, 旧事实不删除：标成已被取代，记下失效时间并指向新事实，版本链因此可追溯。, 记一次被用到：访问次数和最近访问时间是遗忘策略的输入。, to_iso()

### Community 24 - "Agent Proxy Borrowing"
Cohesion: 0.15
Nodes (9): cmd_serve(), HostModels, 接入层借给记忆系统的模型。names 是显示用的名字，会写进 trace 和评测配置。, Borrowed, BorrowedChat, MemoryProxy, agent 最近一次请求里带来的 key 和模型名，记忆模型借来用。只在内存里。, 满足 ChatModel 协议：每次调用时按 agent 最近用的模型和 key，去同一个上游调用。 (+1 more)

### Community 25 - "Update Functions"
Cohesion: 0.28
Nodes (9): Fact, Decision, is_tentative(), _parse_id(), datetime, 更新：新抽出的事实和已有记忆是什么关系，以及置信度。 对每条新事实：先找可能相关的旧事实（同一主体、同一属性的当前事实，加上向量很像的），…, 裁决一批有候选旧事实的新事实。编号 N 用全局序号，模型只能改这一批里的。 输出不可用时这一批都按"新增"处理：宁可多一条，不能误删。, 在写入事务里调用。返回新事实的编号；判为重复时不新建，返回 None。 (+1 more)

### Community 26 - "Anthropic Client Functions"
Cohesion: 0.20
Nodes (10): AnthropicClient, Any, KeySource, Message, OpenAI 风格的消息列表 → (system 文本, 只含 user/assistant 的 messages)。, client 只在测试里传，替换掉真实的 SDK 客户端。, split_system(), 去掉部分推理模型部署放在回复开头的 <think>…</think>，免得思考过程混进答案和 hypothesis。 (+2 more)

### Community 27 - "Llm Functions"
Cohesion: 0.22
Nodes (8): _backoff(), _CallStats, OpenAICompatClient, 走 OpenAI 兼容接口的对话 + embedding 客户端，带重试和用量记账；同时满足上面两个协议。 api_key 不为空时优先于…, 协议适配器共用会话与客户端标识，各自提供所需鉴权头。, 指数退避加随机抖动：大约 1、2、4、8 秒，封顶 30 秒。, 只认秒数写法的 Retry-After；HTTP 日期写法少见，交给指数退避。, _retry_after_seconds()

### Community 28 - "Database Query Results"
Cohesion: 0.19
Nodes (4): 一次取完的查询结果，用法和游标一样（迭代、fetchone、fetchall、lastrowid）。, 回答用的近期对话。主动遗忘后屏蔽受影响会话已整理的前缀，原始审计日志仍保留。, _Rows, Row

### Community 29 - "Question Planning Coverage"
Cohesion: 0.24
Nodes (7): Item, Candidate, QueryPlan, Retriever, hit(), fts_query(), 把若干关键词或短语拼成 FTS5 的 OR 查询；每个词加引号，避免特殊字符被当成语法。

### Community 30 - "Leakcheck Functions"
Cohesion: 0.33
Nodes (9): check(), Leak, project_files(), prompt_texts(), 评测数据泄漏检查：提示词和代码里不能出现评测题的内容（手册 F.1：不得针对评测题硬编码、不得对测试集过拟合）。 检查三类东西： 1.…, Walk for Hunger on February" → 所有首尾都是大写词、至少含两个大写词的连续片段。, _sub_names(), to_text() (+1 more)

### Community 31 - "Cli Functions"
Cohesion: 0.24
Nodes (7): cmd_chat(), datetime, TextIO, 演示用的模拟时钟：/time +3w 就能"过去三周"，几分钟内演示几个月里发生的事。, RealClock, _repl(), SimClock

### Community 32 - "Maintenance Functions"
Cohesion: 0.42
Nodes (9): cmd_maintenance(), apply_cleanup(), cleanup_plan(), integrity(), queue_repair(), 旧库补偿与可预览的物理维护；现有用户库必须显式选择并应用计划。, repair_plan(), _seal() (+1 more)

### Community 33 - "Lifecycle Functions"
Cohesion: 0.44
Nodes (8): _idle_days(), datetime, 巩固与遗忘：按保留分把长期不用的记忆降级归档，超过容量上限时再淘汰分数最低的。…, 给 /stats 用：每条活跃记忆的保留分。, retention(), retention_table(), run_lifecycle(), over_capacity()

### Community 34 - "Memory Source Storage"
Cohesion: 0.28
Nodes (3): Episode, 替换一个轮次的分段索引；调用方在原文写入的同一个事务里提交。, 还没抽过事实的轮次（已遗忘的不算），按时间排好。

### Community 35 - "Llm Functions"
Cohesion: 0.32
Nodes (3): 线程安全地累计每次模型调用。给了 log_path 就边跑边追加 JSONL，中途中断也不丢账。, 按 purpose、model 或某个标签分组汇总；按标签分组写成 "tag:question_id"。, UsageLedger

### Community 36 - "Opencode Go Functions"
Cohesion: 0.39
Nodes (7): configure(), model_spec(), prioritized_ids(), profile_name(), Any, OpenCode Go 型号目录：海外型号优先，协议和推理参数按型号配置。, 更新给定配置；保留其他服务商、embedding 和记忆参数。

### Community 37 - "Timeutil Functions"
Cohesion: 0.33
Nodes (3): 用于时间过滤和排序的日期：有事件时间用事件时间，否则用记录时间。, DateSpan, 一个日期区间及其精度：day、month 或 year。

### Community 38 - "Vector Math Backends"
Cohesion: 0.33
Nodes (6): dot(), 向量小工具。单题的记忆规模在几百条以内，纯 Python 暴力余弦就够快，不需要向量数据库。, query 和 matrix 每一行的点积；两边都已归一化时就是余弦相似度。, scores(), numpy, operator

### Community 39 - "Hybrid Memory Retrieval"
Cohesion: 0.40
Nodes (4): episode_excerpt(), _ordinal_ranges(), Locate literal numbered entries requested by an ordinal, without renumbering…, 预算内选择原文窗口；保留说话人和原始顺序，明确标记省略。

### Community 40 - "Judge Functions"
Cohesion: 0.50
Nodes (3): JudgeIncomplete, RuntimeError, 有题评分失败（模型调用出错等）。其余题的结果已经写好，重跑同一命令只补失败的题。

## Knowledge Gaps
- **1 isolated node(s):** `Provider`
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 329 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Store` connect `Memory Source Storage` to `Maintenance Functions`, `Paged Numeric Evidence`, `Core Functions`, `Lifecycle Functions`, `Memory Source Storage`, `Timeutil Functions`, `Write Functions`, `Benchmark Cache Scales`, `Persisted Memory Schema`, `Memory Settings`, `Multilingual Keyword Processing`, `Memory Source Storage`, `Database Query Results`, `Memory Source Storage`, `Update Functions`, `Database Query Results`, `Question Planning Coverage`?**
  _High betweenness centrality (0.178) - this node is a cross-community bridge._
- **Why does `ModelError` connect `Llm Functions` to `Paged Numeric Evidence`, `HTTP Protocol Streaming`, `Core Functions`, `Benchmark Provenance Methods`, `Write Functions`, `Retrieval Scale Measures`, `Transport Protocol Clients`, `Benchmark Cache Scales`, `Model Discovery Fallbacks`, `CLI Benchmark Commands`, `Fake Functions`, `Mcp Functions`, `Judge Functions`, `Agent Proxy Borrowing`, `Anthropic Client Functions`, `Llm Functions`, `Question Planning Coverage`?**
  _High betweenness centrality (0.115) - this node is a cross-community bridge._
- **Why does `MemorySystem` connect `Core Functions` to `Paged Numeric Evidence`, `HTTP Protocol Streaming`, `Benchmark Provenance Methods`, `Write Functions`, `Retrieval Scale Measures`, `HTTP Protocol Streaming`, `Memory Source Storage`, `Transport Protocol Clients`, `Benchmark Cache Scales`, `Llm Functions`, `CLI Benchmark Commands`, `Memory Settings`, `Fake Functions`, `Mcp Functions`, `Agent Proxy Borrowing`, `Update Functions`, `Question Planning Coverage`, `Cli Functions`, `Llm Functions`?**
  _High betweenness centrality (0.102) - this node is a cross-community bridge._
- **Are the 11 inferred relationships involving `Store` (e.g. with `MemorySystem` and `retention_table()`) actually correct?**
  _`Store` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 23 inferred relationships involving `MemorySystem` (e.g. with `Runner` and `ChatApp`) actually correct?**
  _`MemorySystem` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Are the 18 inferred relationships involving `ModelError` (e.g. with `paged_aggregate()` and `_semantic_finish()`) actually correct?**
  _`ModelError` has 18 INFERRED edges - model-reasoned connections that need verification._
- **Are the 14 inferred relationships involving `MemorySettings` (e.g. with `run_mechanisms()` and `Runner`) actually correct?**
  _`MemorySettings` has 14 INFERRED edges - model-reasoned connections that need verification._

## Parser diagnostics

[graphify] MultiDiGraph edge-collapse diagnostic
input: <in-memory>
input_stage: provided JSON (normal graph.json is post-build)
effective_directed: <direct-call>
nodes: 940
unverified_code_nodes: 0
raw_edges: 2979
valid_candidate_edges: 2780
missing_endpoint_edges: 0
dangling_endpoint_edges: 199
external_reference_edges: 0
self_loop_edges: 8
exact_duplicate_edges: 0
directed_unique_endpoint_pairs: 2605
directed_same_endpoint_collapsed_edges: 175
undirected_unique_endpoint_pairs: 2596
undirected_same_endpoint_collapsed_edges: 184
same_endpoint_group_count: 166
relation_variant_groups: 156
source_file_variant_groups: 0
source_location_variant_groups: 2
context_variant_groups: 8
post_build_graph_type: Graph
post_build_edges: 2794
producer_suppression_sites: 12
producer_suppression_examples:
  - L1646 seen_ids arity=unknown
  - L2179 seen_ids arity=unknown
  - L2181 seen_doc_refs arity=unknown
  - L2551 seen_ids arity=unknown
  - L2698 seen_ids arity=unknown
  - L3424 seen_keys arity=unknown
  - L3593 seen_keys arity=unknown
  - L5669 seen_ids arity=unknown
examples:
  - memsys_bench_scale_build_store -> memsys_store_store edges=3 relations=['calls', 'references', 'uses'] locations=['L55', 'L58'] contexts=['', 'call', 'return_type']
  - memsys_bench_scale_measure -> memsys_config_memorysettings edges=3 relations=['calls', 'references', 'uses'] locations=['L91', 'L92', 'L94'] contexts=['', 'call', 'generic_arg']
  - memsys_cli_fake_models -> memsys_config_models edges=3 relations=['calls', 'references', 'uses'] locations=['L72', 'L74'] contexts=['', 'call', 'return_type']
  - memsys_discover_hash_embedder -> memsys_fake_hashembedder edges=3 relations=['calls', 'references', 'uses'] locations=['L74', 'L75'] contexts=['', 'call', 'return_type']
  - memsys_discover_resolve_models -> memsys_config_models edges=3 relations=['calls', 'references', 'uses'] locations=['L162', 'L163', 'L232'] contexts=['', 'call', 'return_type']
note: normal graph.json is post-build; raw producer loss must be measured earlier.

These are parser/reference representation warnings, not application failure counts.
