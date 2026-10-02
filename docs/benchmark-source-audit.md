# Benchmark 源码与既有评测审计

审计日期：2026-10-02。本文是 `benchmark-methodology.md` 的源码依据，记录本次只读研究发现；不把历史文档中的数字视为重新验证过的结果。审计期间未调用外部模型或搜索 API，未读取或输出密钥，也未改业务代码。

## 实际执行的本地验证

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -B -m pytest --collect-only -q -p no:cacheprovider
.\.venv\Scripts\python.exe -B scripts/eval/validate_datasets.py
```

第一条退出码 0，输出 `442 tests collected in 3.91s`。这证明当前测试可收集，不证明 442 个测试全部通过。第二条退出码 0：商品数据集 67 条、品类数据集 22 条，结构、商品存在性以及部分价格/送达约束自检通过。它不证明语义标签经独立人工审定。README:137 的 137 个单测是旧计数。

写本文前再次读取方法计划，尝试 `git status --short` 返回 `fatal: not a git repository`；当前目录缺少可用 Git 元数据，版本追溯应使用文件 SHA256，而不能编造 commit。

## 工具注册、ToolSearch 与 MCP

当前主 Agent 在 `app/application/agents/main_agent.py:106-159` 静态装配 Toolkit：业务工具、四个 Task 计划工具、task_dispatch、记住/删除偏好。搜索与交易工厂也静态注册工具，见 `search_agent.py:59-105`、`trade_agent.py:61-100`。`permissions.py:22-59` 对已知工具添加 allow 规则。

对 app、scripts、tests、README、docs 检索 ToolSearch/MCP 未找到本项目的业务接入。SDK `.venv/Lib/site-packages/agentscope/tool/_toolkit.py:88-134` 的确接受 `mcps` 和 `tool_groups`，但上述业务装配只传 `tools`。框架支持某能力不能作为项目已经使用的证据。因此本项目目前不能测量或宣称 ToolSearch/MCP 动态发现带来的工具描述缩减收益。

## Prompt Cache 的可测性

`app/infrastructure/llm.py:226-249` 读取 usage 为预算累计总 token，没有应用层缓存命中率统计。真实 provider usage 可以作为测量依据，但不能依赖 SDK 归一化后的零值推断原始字段存在。

- SDK `.venv/Lib/site-packages/agentscope/model/_openai_chat/_model.py:267` 请求 `stream_options.include_usage`。
- 同文件 `:343-355`、`:521-533` 把 `prompt_tokens_details.cached_tokens` 映射为 `cache_input_tokens`，缺字段时默认 0。
- `.venv/Lib/site-packages/agentscope/middleware/_tracing/_extractor.py:340-359` 仅在缓存计数非零时添加相应 span 属性。
- `.venv/Lib/site-packages/agentscope/middleware/_base.py:206-232` 提供 `on_model_call` 测量挂钩，参数含 messages、tools、tool_choice、current_model；但返回的规范化 usage 已经丢失字段是否缺失的信息。

benchmark 必须安全捕获原始 usage，并分别保存字段可用性、输入 token、缓存 token。缓存 token 比例与发生命中的请求比例是两个指标。缓存字段缺失应为 null，不能补 0；一次实验也不能证明“长期稳定 80%”。原始 headers、Authorization 与 credential 不应保存。

## 任务完成率及既有 Agent 回归

已完整阅读 `eval/cases.yaml` 与 `scripts/eval_regression.py`。13 个 case 涵盖预算检索、到手价、比价、下单前确认、下单取消闭环、长期记忆写入/跨会话读取、无货、不存在订单、闲聊、品类知识、长上下文和工具不可用。

`eval_regression.py:134-165` 顺序调用真实意图 HTTP 接口，然后调用 LLM judge；P0 全过且 P0/P1/P2 加权分至少 0.7 才判 PASS。judge 主要看到对话文本和事实表，未直接检查工具调用和数据库状态。因此“已记住”等语言自述不能独立证明写入发生。`:252-256` 最后写 Markdown，没有逐试验 JSONL、重复实验或统计区间。

`:197-215` 已有语义缓存防污染检查。新 benchmark 应延续这种隔离，并为每次重复建立隔离 buyer、session、DB 和库存。原用例 `memory-write` / `memory-recall` 共享固定 buyer；再次执行时若不清晰隔离会受到旧记忆污染。新基线是当前代码的机制消融，并非历史 71% 版本的复原。

补读发现评分器还缺严格 judge schema 验证：`:123-131` 将空档位按满分处理，并按 Python truthiness 判断 pass。只读执行如下命令，两个输出均为 `(1.0, True)`（退出码 0）：

```powershell
.\.venv\Scripts\python.exe -B -c "from scripts.eval_regression import score_case; print(score_case({})); print(score_case({'p0':[{'pass':'false'}]}))"
```

所以缺失全部 rubric 或字符串 `false` 都可能被计为 PASS。旧报告不能直接作为新 benchmark 的 gold；应校验每条预登记 rubric 的覆盖、类型与布尔值，并将不合格评分记为 evaluator error，而非满分。

`scripts/loadtest.py:42-51` 仅把 HTTP 200 当成功，`:64-75` 只统计成功请求的延迟。服务失败可能仍以 200 返回 `[error]`（`app/presentation/server.py:271-292`），所以旧压测成功率不能写成业务完成率，成功条件应由任务契约和状态证据独立判断。

## 并发与独立上下文

`app/application/tools/task_dispatch_tool.py:90-132` 为每次派发创建新子 Agent，并发布开始、结束和 elapsed_ms；`main_agent.py:118-132` 将调度工具注册为 concurrency safe。子代理收到 demands 与可选偏好提示，不自动继承主 Agent 历史。

`scripts/verify_parallel.py:28-36` 使用两种不同 query 分别引导串行和并行；`:112-119` 每组只跑一次且固定先并行后串行。不同问题可能导致不同子任务数量、推理轮数和输出长度，不能据此给出稳定加速百分比。新实验应以同样 demands、任务数量与共享模型闸门，做配对串并行调度，并同时报告实际重叠、完成率和全部尝试。

`tests/test_subagent_preference_inject.py:29-46` 使用 RecordingWorker/RecordingFactory，验证服务端确实注入偏好；它不调用真实模型，也不证明模型遵守偏好或最终推荐质量提升。`tests/test_interview_regressions.py:79-95` 证明推荐服务还会直接读取并执行材质排除，所以最终商品合规不能全部归因于子 Agent 的语言上下文注入。

## 压缩与事实保留

`app/application/agents/context_policy.py:52-67` 配置框架压缩；`critical_facts.py:40-86` 从上下文提取工具事实到侧账本，并在 system prompt 中恢复。必须同时计入摘要、保留上下文和侧账本的成本，不能仅按摘要长度声称缩减。

`tests/test_phase3.py:106-136` 验证阈值、提示词关键词和模板字段；`tests/test_interview_regressions.py:98-115` 人工清空 context、设置遗漏事实的 summary，再验证 ledger 能保留标识和金额。这是机制正确性证据，不是模型真正执行压缩后的事实保留率。真实 benchmark 需调用框架压缩，记录确实执行的证据，并追加事实问答探针。

## Tracing 与故障定位时间

`app/infrastructure/tracing.py:30-46` 在配置 OTLP endpoint 时初始化 provider；`:50-60` 为 Agent 挂 TracingMiddleware。`tests/test_order_queue_regressions.py:140-156,182-198` 验证 traceparent 序列化、事件传播和远端 context attach，没有人工排障计时。

SDK `.venv/Lib/site-packages/agentscope/middleware/_tracing/_trace.py:312-374` 的工具 span 主要按异常处理状态：工具返回 ToolChunk(ERROR) 而没有抛异常时，仍可能标记 span 成功。`app/infrastructure/resilience.py:117-127,161-179` 将超时/异常/熔断写为业务 `tool.result` 错误事件。诊断必须结合业务结果与 span，不能只用 span status 查找所有失败。

自动化可以测关联覆盖、有效 trace ID、错误证据完整性、故障检测延迟与采集开销。人工定位 30→5 分钟必须用真人盲测/交叉实验记录开始和确认根因时间；自动规则定位耗时不能冒充真人定位时间。没有真人样本应明确为未测，而不是填目标数字。

## 召回数据集与旧策略脚本

已完整阅读商品 67 条和品类 22 条 JSONL。商品标签含 55 条 lexical、12 条 semantic，属于已知小种子库的人工用例，不是生产随机样本或未见过的 holdout。

`scripts/eval/run_product_recall.py:70-100` 直接装配旧 CatalogSearchUseCase，缺向量会实际降级，缺 reranker 时 embedding_rerank 实际等价 embedding_only。`:269-274` 的聚合名仍按请求策略保存；因此新报告必须逐次记录实际 `recall_strategy` / `rerank_applied`，不能将请求标签当实际执行证据。当前主业务采用 ProductRecommendationService，旧脚本结果只能标旧模块范围。

`scripts/eval/metrics.py:47-87` 提供 Recall@K/MRR/NDCG，`:121-140` 使用宏平均；NDCG 使用标注顺序的线性 gain。`run_product_recall.py:103-132` 的 filter_ok 检查送达泄漏及相关项被 filtered_out 误杀，没有独立检查所有超预算泄漏，不能将其扩大为所有硬约束准确率。

`tests/test_retrieval.py:20-45` 使用词轴 embedding 和反序 reranker 桩；这些测试只能证明接线与策略行为，不能证明真实语义/重排效果。当前数据集验证通过不取消这些限制。

## 既有 live 脚本的范围

- `scripts/verify_product_recommendation_live.py:39-83` 调真实商品发现、embedding、推荐服务；`:53` 明确不是完整 LLM Agent 对话。它隔离商品写入，复用捕获的外部结果做预算诊断，报告这种 replay 是正确做法。
- `scripts/verify_web_knowledge_live.py:31-49,80-159` 捕获真实网页来源、验证门槛/去重/重开后召回；第三方失败记录错误类型。此脚本验证网页知识服务，不能自动视为当前购物主链。
- `scripts/smoke_e2e.py` 验 HTTP 与 WS 事件链路并打印最终回复，未定义机器业务成功断言。
- `scripts/locustfile.py` 测同步/异步 WS 时延；使用外部 locust/websocket-client，当前 pyproject dev 依赖未声明这两个包。异步场景看到 error 即退出，没有将业务失败显式作为成功率分母处理。

## 补读全部既有 tests 后的证据边界

以下补充不改变预登记实验方向，但限制最终报告可以作出的推断：

- `tests/test_embedding_client.py:4-10` 的旧背景注释使用“单批超过 10 条”“事故”“线上”等表述；实际正文 `:29-38,40-85` 是 MockTransport 和显式 `_MAX_BATCH` 的测试。不能由注释或该测试推导真实网关严格阈值、生产事故及历史提升。现有默认批大小与历史故障现象要分开。
- `tests/test_local_embedding.py:14-28` 使用 FakeBackend，且阻止真实 HTTP；`:32-40` 证明适配契约，不能证明 BGE 真推理质量。`test_preference_selector.py:16-24,102-111` 的相关性也来自词轴桩，不是语义质量评测。
- `tests/test_phase4_cache.py:18-84` 使用内存 Redis/embedding 替身；其命中与偏好 scope 测试证明应用缓存契约，不是 provider Prompt Cache 命中率。
- `tests/test_phase4_model.py:169-219` 明确区分首分片前失败可回退、部分输出后失败不可重播以及首分片不等流结束。故障 benchmark 应将“正确停止且没有重复输出”和“业务完成”作为不同结果，不能为了提高完成率重播部分流。
- `tests/test_phase4_sql.py:145-155` 的 events_persisted_with_payload 仅调用写入并确认未创建 session，没有读回事件 payload。真实事件/span 完整性必须检查 benchmark 导出的记录。其 SQLite 内存库测试也不能外推 PostgreSQL/MySQL 特性。
- `tests/test_phase4_queue.py:28-84` 与 `test_harness_infra.py:20-68` 是行为 fake；真实 Redis 证据入口另见 `test_queue_redis_integration.py:8-41`、`test_shared_breaker_atomic.py:8-47`、`test_interview_regressions.py:118-168`，需要显式隔离 Redis 或可用 Redis >= 6.2，未配置时可能 skip。收集到这些测试不表示执行过跨进程/原子性验证。
- `tests/test_persistent_product_catalog.py:31-40` 明确外部商品只提供购买链接，不能在本地创建演示订单。benchmark 的下单闭环必须使用本地种子商品，且只能称本地演示交易流程。
- `tests/test_web_knowledge.py:198-259` 使用真实本地 AgentScope/Qdrant，但 embedding 与来源仍是确定性替身。因此“真实向量存储集成测试”不能写成“真实网页检索语义质量”。
- `scripts/eval/run_category_recall.py:69-91` 先检索 top_k 个 chunk 再去重为文档，文档数可能小于 K。报告应说明这是 top_k chunk 导出的文档 Recall，而不是重新取满 K 篇唯一文档；不得与商品 Top-K 不加区分地聚合。

## 完整阅读覆盖

已通读的核心文件：

- `pyproject.toml`；本次 benchmark 新建前全部 13 个 scripts Python 文件：`scripts/__init__.py`、`scripts/eval_regression.py`、`scripts/verify_parallel.py`、`scripts/loadtest.py`、`scripts/locustfile.py`、`scripts/smoke_e2e.py`、`scripts/verify_product_recommendation_live.py`、`scripts/verify_web_knowledge_live.py`、`scripts/eval/__init__.py`、`scripts/eval/metrics.py`、`scripts/eval/validate_datasets.py`、`scripts/eval/run_product_recall.py`、`scripts/eval/run_category_recall.py`。
- `eval/cases.yaml`、`eval/product_recall.jsonl`、`eval/category_recall.jsonl`；`scripts/eval/metrics.py`、`scripts/eval/validate_datasets.py`。
- `app/application/agents/main_agent.py`、`search_agent.py`、`trade_agent.py`、`permissions.py`；`app/application/tools/task_dispatch_tool.py`；`app/infrastructure/llm.py`、`tracing.py`、`eventbus.py`。
- 本次 benchmark 新建前全部 34 个 tests Python 文件，包含 33 个测试模块和 `tests/__init__.py`，具体清单见下表。包含 fixtures、替身、测试正文、skip 条件与清理逻辑，已完成全文阅读。

| 范围 | 已全文阅读文件（相对 tests/） |
|---|---|
| 领域、用例、工具 | `__init__.py`, `test_domain.py`, `test_pricing.py`, `test_usecases.py`, `test_tools_and_eventbus.py` |
| 模型与基础设施 | `test_phase3.py`, `test_phase4_cache.py`, `test_phase4_model.py`, `test_phase4_throttle.py`, `test_phase4_queue.py`, `test_phase4_sql.py`, `test_queue_redis_integration.py`, `test_shared_breaker_atomic.py`, `test_order_queue_regressions.py` |
| Harness | `test_harness_drift.py`, `test_harness_guards.py`, `test_harness_infra.py`, `test_harness_middleware.py` |
| 上下文及偏好 | `test_interview_regressions.py`, `test_memory_persistence.py`, `test_preference_selector.py`, `test_preference_tools.py`, `test_subagent_preference_inject.py` |
| 检索、商品与 embedding | `test_embedding_client.py`, `test_local_embedding.py`, `test_retrieval.py`, `test_recall_metrics.py`, `test_product_recommendation.py`, `test_product_pipeline_wiring.py`, `test_persistent_product_catalog.py`, `test_external_discovery.py` |
| 网页 | `test_web_knowledge.py`, `test_web_product_policy.py`, `test_web_source.py` |

另对 README/docs 完成评测、缓存、工具、tracing 相关检索，按相关段落读取 SDK 模型/Tracing 实现（具体行号见上文）。全部业务文件的阅读由同轮其他模块审计补充。本次审计期间新建的 `scripts/benchmark/` 与 `tests/test_benchmark.py` 由主代理审阅，不计入这里的既有代码阅读清单，也不把原来的 442 collected 自动套用到新增后的测试总数。

后续 benchmark 若修改测量器，需要保留失败 pilot，正式试验开始前冻结 case、配置、源码哈希与计分规则。不要按试验结果更换标签、筛除失败或切模型以获得更好数字。
