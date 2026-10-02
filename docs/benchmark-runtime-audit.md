# Benchmark 运行代码审阅

审阅日期：2026-10-02。本文是 `benchmark-methodology.md` 的运行实现补充。来源为当前工作区代码；以下均为代码检查所得，**不是已经运行出的实验结果**。本次审阅未调用第三方 API、未输出凭据、未修改业务代码。这里只保存研究结论，不实现 benchmark。

## 1. 实验隔离

| 实现事实 | Benchmark 约束 | 当前代码来源 |
| --- | --- | --- |
| JSON preference/session/conversation 和 external product overlay 写到 data_dir | 每 run 使用新 DATA_DIR；不要复用用户工作数据。明确给出独立数据库 URL，不能假设 DATA_DIR 已经隔离所有存储 | `app/infrastructure/persistence/json_file_stores.py:30`；`persistent_product_repository.py:18`；`sql/repositories.py:53` |
| 商品、品类 KB、web KB 分别落到 qdrant、qdrant_kb、qdrant_web_kb；已有 collection 不检查模型身份 | 隔离全部三个路径；模型切换时使用新 collection/目录；保存 embedding 模型、维度、目录与语料 hash | `app/infrastructure/vector/qdrant_product_index.py:27`；`app/infrastructure/rag/category_knowledge.py:46` |
| 语义缓存命中直接复用回复，embedding 装饰器另外统计 hits/misses | Agent 行为实验禁用 SEMANTIC_CACHE_ENABLED；embedding 缓存冷热另外记录。两者均不能充当供应商 Prompt Cache 命中率 | `app/infrastructure/cache/semantic_cache.py:2`、`:20`；`cached_embedding_client.py:27` |
| Redis queue、breaker、embedding 等使用固定前缀 | 新 session 不足以隔离；使用专用 Redis 实例/数据库，或明确不测 Redis 路径。不得清理用户已有共享 Redis | `app/infrastructure/queue/redis_stream_queue.py:24`；`app/infrastructure/shared_breaker.py:29`；`app/infrastructure/cache/cached_embedding_client.py:30` |
| 内存产品仓储返回可变原对象，扣库存会改变后续输入 | 每配对 trial 重新构造 seed products 与仓储；不要让 treatment 接受 baseline 改过的库存 | `app/infrastructure/persistence/in_memory_repositories.py:19`；`app/domain/catalog/sku.py:30` |
| SQL 订单有持久库存、幂等记录，JSON 外部目录有增量 overlay | 新数据库与新外部商品目录；订单失败、取消后都检查真实库存，不只看模型文本 | `app/infrastructure/persistence/sql/tables.py:137`；`sql/repositories.py:248`；`persistent_product_repository.py:24` |
| queued 同 session 同 query 600 秒内按指纹去重 | 独立 buyer/session/request ID；仅测试记忆跨会话时有意复用 buyer，测试幂等时有意复用 request ID | `app/presentation/server.py:49`、`:233` |
| 本地 embedding 全局缓存模型，使用全局 inference lock；ONNX 配置 2 CPU threads | 冷启动与 warm run 分开；多个 asyncio 请求并不代表 embedding 真并行 | `app/infrastructure/embedding/local_embedding.py:23`、`:43`、`:80` |
| 品类 KB 按文件 stem 跳过已存在文档，不按内容 hash 更新 | 新实验用新索引；冻结 knowledge 文件版本；不要让已更新文档与旧索引混用 | `app/infrastructure/rag/category_knowledge.py:73` |
| token budget 会在 main/lite/minimal/fallback 间路由 | 若不测预算，关闭预算；若开启，固定总量并逐请求记录模型、budget charges 和 tier | `app/infrastructure/budget.py:31`、`:49` |

## 2. 可信评分来源

### 订单

真实业务判据应来自 order repository、product inventory 和订单快照，而非自然语言中的“已成功”。核验 buyer、product/SKU、quantity、CONFIRMED/CANCELLED 状态、金额、库存差值。幂等重放应返回相同订单并只扣一次库存；取消重放应只回补一次。代码支持内存进程内保护与 SQLite 事务路径，需分别标明实际运行哪条路径。

来源：`app/application/usecases/order_usecases.py:34`、`:74`、`:108`；`app/infrastructure/persistence/sql/repositories.py:249`；`app/domain/order/order.py:84`。

订单是本地模拟意向单，不包括支付和真实物流。货币换算是静态快照，运费与关税为演示规则；JPY 也被统一按 100 倍处理。只能测“是否符合本项目规则”，不能报告真实跨境税费准确率。来源：`app/domain/order/order.py:2`；`app/domain/catalog/exchange_rate.py:13`；`money.py:29`；`app/domain/shipping/tariff_schedule.py:29`。

订单工具先对 quantity 调用 `int`，随后 usecase 的严格整数检查看到的是转换值。评分应保存原始工具入参并对照用户需求，避免把 true/小数经转换后认成正确数量。来源：`app/application/tools/order_tools.py:62`。

### 长期记忆

保存与删除必须读取 preference store 验证最终状态，并在新 session 检验后续行为。删除是 statement 精确匹配；相近文字不能算同一事实。**未找到目标而没有删除任何内容也返回 ToolResultState.SUCCESS**，所以不能只统计工具状态。来源：`app/domain/buyer/preference.py:45`；`app/application/tools/forget_preference_tool.py:69`、`:81`。

JSON preference 去重按 buyer+kind+statement；同 statement 的 like 和 dislike 可以同时存在，删除按 statement 会移除两者。暂时例外与持久删除在工具说明中区分，应分为不同测试。来源：`app/infrastructure/persistence/json_file_stores.py:38`、`:59`；`app/application/tools/forget_preference_tool.py:26`。

### 检索与推荐

应保存 hits、ID 顺序、recall_strategy、rerank_applied、filtered_out，并按冻结的标注集评分。旧 CatalogSearchUseCase 固定向量召回 top_n=8 后过滤，关键词路径扫描所有商品，因此候选量不相同。embedding 返回空列表可触发关键词降级，reranker 未配置或失败不会算真正精排。来源：`app/application/usecases/catalog_search.py:40`、`:104`、`:178`、`:212`。

该旧 usecase 的 price cap 按主 SKU 商品价过滤，不是到手价；category 仅在关键词分数中加权；它不应用 ProductSearchSpec 中 excluded_brands/materials。完整实验要沿当前 ProductRecommendation 接线评分，不能用旧 usecase 的局部结果替代完整 Agent 能力。来源：`app/application/usecases/catalog_search.py:151`、`:172`、`:224`；`app/domain/catalog/product_search_spec.py:29`。

种子数据文件明确记载：扩充语料时刻意避开 P1001 在“旅行三件套 抗造”上的正面竞争以保留既有 top-1 测试。因此已有语料与既有 query 属于 development corpus，不能声称独立 holdout。保持目录原样，冻结新的测试问法、覆盖全目录和负例，公布来源与限制；不可再调语料让某实验臂获胜。来源：`app/infrastructure/persistence/seed_products.py:16`。

### 外部证据

外部商品的 `price_verified=True` 表示解析到明确价格依据，同一结构仍有 `source_confidence=unverified` 与 `source_verification=unknown`；不代表商家认证或实时价格核验。外部商品 SKU stock=0 是建模占位，availability 默认 unknown，不可据此说确认缺货。评分应测回答是否忠于捕获的商家证据，缺失运税时是否诚实报告未知。来源：`app/infrastructure/catalog/external_discovery.py:122`、`:129`、`:156`。

真实网页搜索和内容会变化；若因第三方收费/不稳定而 replay，保存原始响应与采集时间/hash，并将该臂标为第三方响应回放，不能标 live discovery。商品 URL admission 是保守路由、标题、型号和价格/购买证据规则，不是商家真实性判定。来源：`app/application/usecases/web_product_policy.py:1`、`:45`；`app/infrastructure/rag/web_source.py:44`。

WebKnowledgeService 要求本地检索非空才开放 admission，以本地末位相似度为 threshold，web score 再乘权重；它测相关性，不测事实真伪。保存 gate、raw_score、weight、admitted_count、失败状态，并隔离写回后的 web KB，避免后跑实验继承先跑 arm 的新证据。来源：`app/application/usecases/web_knowledge.py:80`、`:97`、`:120`、`:217`。

## 3. 执行成功与用户可见成功

- `SubmitIntentResponse` 只有 session_id 与 final_text，没有 success/error。HTTP 200 不是任务完成；推荐从 orchestrator 结构化结果和事件采集，再使用独立业务判分。来源：`app/presentation/dto.py:21`；`server.py:130`。
- `/health` 的 status 总是 ok，依赖异常放在 database/redis 字段；仅检查 status 不足以证明实验依赖就绪。来源：`app/presentation/server.py:105`、`:121`。
- 前端只从 WebSocket final.result 显示最终回复，fetch 不检查 HTTP 状态也不使用响应 JSON。因此“后端完成”和“用户看见回复”是不同指标。来源：`frontend/src/App.tsx:64`、`:86`。
- 浏览器 localStorage 保留 buyer/session，刷新不是新会话；UI benchmark 应显式控制浏览器存储。来源：`frontend/src/App.tsx:8`、`:23`。
- WebSocket 订阅按 session，而队列同步等待只看到同 session 的 final.result 就返回，未按 task ID 匹配。主 benchmark 不应并发复用同一个 session，否则可能取到另一请求答案。来源：`app/presentation/connection.py:22`；`server.py:271`、`:290`。

## 4. 并发、重试和观测边界

- WebKnowledgeService 持有实例锁覆盖整次 search，包括远程发现；订单也持有 repo 级 placement lock。应采集实际开始/结束与重叠时间，不能从 gather 本身推出并行生效。来源：`app/application/usecases/web_knowledge.py:79`；`order_usecases.py:46`。
- Redis depth 是未读 lag，退回 pending 只是旧版本兼容；queue_position 使用总 depth，不是精确排队位置。不要将它当排队等待时长或已排第几名。来源：`app/infrastructure/queue/redis_stream_queue.py:73`、`:82`。
- worker 收到 result.success=False 写 failed，但不抛异常；handler 正常返回后队列 ACK。只有异常会进入 delivery retry。因此业务失败率、工具 attempt retry、worker delivery retry 需要分别统计。来源：`app/worker.py:33`、`:47`；`app/infrastructure/queue/redis_stream_queue.py:198`。
- shared breaker Redis 出错时 allow_async 直接放行；success/failure 记录可回落本地。运行 Redis 故障实验时，应记录 shared backend 实际可用性，不能仅因配置启用就称熔断共享生效。来源：`app/infrastructure/shared_breaker.py:105`、`:119`。
- Windows worker 的 loop.add_signal_handler 可能不受支持；这是待真实启动核验的兼容风险，不是本次已复现错误。若运行时出现，先确认异常，再在获准阶段处理。来源：`app/worker.py:71`。
- JSON conversation 的 limit 返回末尾记录；SQL 按升序再 limit，返回最早记录。评分从完整原始采集获取，避免长历史截断语义差异。来源：`app/infrastructure/persistence/json_file_stores.py:174`；`sql/repositories.py:168`。
- 安全工具输出过滤与输出脱敏是正则规则，不能等同语义级注入检测/全量安全证明。安全测试应同时含匹配、同义改写、正常文本误报，并注明有限威胁模型。来源：`app/infrastructure/security/content_filter.py:27`；`output_guard.py:45`。
- tracing/事件字段只能支持 trace 完整性、故障信息覆盖或自动归因评分；“人工定位 30→5 分钟”需要预注册人工盲测、起止定义和人员/顺序控制。代码存在事件/持久化不构成该时间的测量证据。

## 5. 已读文件覆盖

以下为本审阅负责并完整读取的业务源文件；未将 pycache、node_modules、打包 dist 当作项目源代码。父任务其他审阅另负责 README/docs、agents/context/memory/resilience/tracing/settings/composition、tests、ToolSearch/MCP 和 product_recommendation。

- `app/domain/` 全部 `.py`：buyer/preference；catalog 的 exchange_rate、money、product、product_search_spec、sku 及 product_repository/retrieval_ports；order 的 address、order_line、order 及 order_repository；queue/task_queue；session/conversation_store、session_store；shipping/tariff_schedule；其 `__init__.py`。
- `app/infrastructure/persistence/`：in_memory_repositories.py、json_file_stores.py、persistent_product_repository.py、seed_products.py、sql/repositories.py、sql/tables.py。
- `app/infrastructure/cache/`：cached_embedding_client.py、redis_cache.py、semantic_cache.py、`__init__.py`。
- `app/infrastructure/budget.py`、`shared_breaker.py`。
- `app/infrastructure/security/`：content_filter.py、output_guard.py、`__init__.py`。
- `app/infrastructure/embedding/`：factory.py、local_embedding.py、openai_embedding_client.py、`__init__.py`。
- `app/infrastructure/vector/`：index_bootstrap.py、qdrant_product_index.py、`__init__.py`。
- `app/infrastructure/rerank/`：http_reranker.py、`__init__.py`。
- `app/infrastructure/rag/`：category_knowledge.py、web_source.py、web_vector_store.py、`__init__.py`。
- `app/infrastructure/catalog/external_discovery.py`。
- `app/infrastructure/queue/redis_stream_queue.py`；`app/worker.py`。
- `app/application/usecases/`：catalog_search.py、order_usecases.py、web_knowledge.py、web_product_policy.py。
- `app/application/tools/`：order_tools.py、remember_preference_tool.py、forget_preference_tool.py。
- `app/presentation/`：connection.py、dto.py、server.py。
- `frontend/src/`：App.tsx、main.tsx、types.ts、styles.css、components/EventTimeline.tsx、components/ProductCards.tsx。

## 6. 结论如何用于方法计划

可以严格实现的评分应以业务状态、冻结事实、真实调用 usage、真实时间戳和保存的原始事件为基础。每条记录都应保存实验臂、样本 ID、独立运行编号、版本、配置摘要、数据 hash、依赖模式与实际降级路径。统计按阶段和依赖模式分组，不能将局部工具速度、Agent 完成率、缓存复用率、真实人类定位耗时混为同一指标。

本文提出的是实现约束与待测风险。没有提前假定现有简历数字正确，也没有给出预设胜者或将代码正确性检查当作真实量化成果。
