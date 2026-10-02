# Architecture and benchmark audit

审计日期：2026-10-02。本文件在新增实验代码之前形成。依据当前源码、运行时配置、既有测试与旧实验原始记录；旧审计只作导航，不以 README 的功能描述代替接线证据。目录无 Git 元数据，commit/dirty 均应记录 unknown，以文件哈希追溯。

## Runtime 接线与可测性

下表位置均相对仓库根目录。active 区分“代码接入”和“本机配置实际启用”；不会将实验专门启用的功能称为部署默认。

| Feature / source code location | Runtime path / actually active | Engineering purpose | Possible failure mode | Worth benchmarking / suggested metric |
|---|---|---|---|---|
| Main ReAct / application/agents/main_agent.py | server → orchestrator → SessionRegistry → Agent.reply_stream；主 Agent 持有全部业务工具，max_iters=15 | 简单任务少走一层代理 | 无条件派发、错误拆解、轮次耗尽 | 是：完整任务契约、调用数、token、延迟；无独立复杂度分类器 |
| Expert / search_agent.py, trade_agent.py | task_dispatch_tool.py 每次新建实例；Search max_iters=6 | 子任务隔离、专用工具集 | demands 漏约束，专家失败仍被包装成成功 ToolChunk | 是：约束保留、真实工具结果、父任务成功，不只看 dispatch 返回 |
| Task plan / main_agent.py | SDK TaskCreate/Update/Get/List 挂 state.tasks_context | 显式计划状态 | 计划文本与执行不同步 | 次级：计划完成与业务完成一致性 |
| On-demand routing / prompts/globex.yml | 提示词规定简单直做、复杂派发；无硬路由阈值 | 避免简单任务派发开销 | 模型不遵守建议，模型版本影响决策 | 探索性；main-only/always-dispatch 必须标实验策略，不能伪称现成开关 |
| Task-specific context / tools/task_dispatch_tool.py | 子代理只收 demands；服务端补检索偏好；不自动继承历史 | 减少无关历史与旧约束干扰 | 当前约束在 parent 中但漏抄到 demands；偏好读取失败只告警 | 优先：同需求配对的 token、遗漏约束、工具参数、任务正确性 |
| Parallel dispatch / main_agent.py | FunctionTool concurrency_safe → SDK gather，共享 throttle | 独立子任务重叠执行 | 有依赖任务并行、闸门争用、并行失败汇总丢失 | 值得；同 demands、同子任务数，检查实际时间重叠与全部失败 |
| Product recommendation / composition.py → usecases/product_recommendation.py | 当前主路径；全目录规则/concept fit 与 dense similarity 取 max，再硬过滤和加权 | 先验证业务约束再排序 | 每次重复 embed 商品；embedding 异常被捕获后退回 lexical，缺少明确策略告警 | 是：约束违规/空结果/召回/时延。不是 BM25，也不是 Qdrant hybrid |
| Legacy vector/reranker / catalog_search.py, vector/, rerank/ | startup 仍建产品 Qdrant；reranker 被构造，但未传入当前 ProductRecommendationService | 旧两阶段召回 | 建库有成本但购物排名不使用此索引；旧脚本测错模块 | 不将旧模块提升归于现主链；先区分实际路径 |
| Category RAG / rag/category_knowledge.py | category_insight_tool → KnowledgeBase → Qdrant | 品类知识引用 | 文档 top-k 与 chunk top-k 混淆；不能拿文章当购买页 | 次级：文档召回和引用证据 |
| External discovery / catalog/external_discovery.py | 本机 Tavily 配置存在；local 不足才发现；真实网页商品提取 | 获得目录外商品 | 用户明确要求联网仍可能 local_sufficient；抓取失败变 discovery_failed | 是：真实商品交付、同 SKU 报价证据、unknown 保留 |
| Web knowledge / usecases/web_knowledge.py | composition 中 web_knowledge_service=None，不在正式购物链 | 旧文章入库能力 | 误把可导入模块当已上线特性 | 当前不做主线 benchmark |
| Metadata constraints / product_recommendation.py | _check_exclusions/_landed/stock/destination/price 都在评分前 | 不用语义相似掩盖业务不合格 | 未知材质导致拒绝；所有预算被解释为到手上限，标价诉求不匹配 | 值得：违规率与误拒率同时报告 |
| Recent + summary / agents/context_policy.py | SDK ContextConfig 0.75 trigger、0.15 reserve；本机128000窗口 | 限制历史增长 | 15–30轮不一定触发默认阈值；摘要需额外调用 | 优先：实际压缩次数、累计输入、约束保留；受控窗口须单列 |
| Structured facts / agents/critical_facts.py | tracing.build_agent_middlewares 无条件挂载；从 tool call/result 捕获到 middle_context | 防止摘要丢金额/ID/搜索条件 | 不是完整业务状态机；latest_user_request 被寒暄覆盖，实体账本无上限；旧事实残留 | 优先消融：summary-only vs 同策略+原有 sidecar；不能预填 gold state |
| Session persistence / main_agent.py, persistence/sql | 每轮持久化 AgentState；恢复异常降级新会话并告警 | 重启后继续任务 | 对话丢失但仍可回应；session/buyer边界需验证 | 是：重启恢复、错误买家注入、pending action |
| Long-term preference / orchestrator.py, preference_selector.py | 每轮 authoritative snapshot；dislike 全量，like top5；本机 relevance off | 新旧偏好替换、相关性选择 | 不支持的 dislike 阻断搜索；临时 override 不能解除服务层旧排除 | 值得后续；跨 session recall/precision/stale/override，不能以口头“记住”判成功 |
| Preference writes / remember/forget tools | SQLite 持久化，忘记精确匹配 | 可撤回的长期记忆 | 当前工具以LLM选择为入口，临时需求被误写 | 写入/删除/误写率，数据库断言 |
| Provider prompt cache / llm.py, preference_selector.py | 保持 system 主体，但 sidecar 会动态改 system；本机无专用缓存控制API | 复用前缀 | 把cached/input当请求命中；不同组共享暖缓存；sidecar破坏前缀稳定性 | 只报告provider字段；system-only/eligible分母不可观测时为NA |
| Redis/semantic cache/queue / composition.py, cache/, queue/ | 本机 Redis 未配置，队列/共享熔断无效；联网存在还会关最终答案缓存 | 复用/削峰/跨进程状态 | 测内存替身不能证明Redis原子性；跨进程trace漏传 | 当前不选主线；不得把配置存在当运行证据 |
| Model retry/fallback / infrastructure/llm.py, transient.py | 模型层+orchestrator两层重试；本机主备同名，实际 fallback=None | 首输出前瞬时失败恢复 | 嵌套重试放大；空文本TimeoutError未匹配；fallback未实际配置 | 优先按故障分层；retry/fallback消融、实际attempt与废调用 |
| Partial stream boundary / llm.py | prime首chunk；输出后失败抛PartialStreamError，不重播 | 防止重复输出/潜在重复动作 | “安全停止”被错误计为恢复成功 | 优先：完成率与安全不重播分开 |
| Tool timeout/breaker / resilience.py | 三工厂工具包装；共享进程内registry，阈值3/冷却60s | 截断慢调用、持续故障快速失败 | 无工具重试；breaker按工具名共用；取消也记失败 | 优先：downstream次数、fast-fail、半开恢复；不以提高成功率为唯一目标 |
| Loop guard / harness_middleware.py, harness/loop_detector.py | Main 自建dispatch/记忆工具挂Harness；从SearchFactory拿的product tool只挂Resilience | 提醒模型停止重复动作 | 仅比较工具名，不比args；只提示不阻断；商品工具当前未挂同样护栏 | 优先负结果：提示数≠阻止数，检测误报与真实装配区别 |
| Schema/sequence / harness/assertions.py | 只在有Harness的工具上生效；key存在校验，非类型校验 | 减少明显错序与坏结果 | 首次/恢复无history只告警；ERROR文本跳过schema；并非事务授权 | 单独报告范围；不称严格schema或确认安全保证 |
| OTEL + events / tracing.py, eventbus.py, orchestrator.py | middleware已挂；本机OTLP未配置，默认无真实导出；实验启用本地exporter | 将请求、模型、工具和错误串联 | ToolChunk ERROR不必然是span ERROR；工具事件缺唯一call ID；retry只有日志 | 值得：关联覆盖、父子完整性、错误定位充分性；无人类分钟指标 |
| Token/drift/output guard / budget.py, harness/drift_detector.py, security/ | token预算=0、drift=false，output guard=true | 控成本/观察偏离/过滤输出 | 未启用不能声称生效；drift轮末只观测不纠正 | 当前不扩展大量参数实验 |
| Orders / order_usecases.py, persistence/sql | 本地订单与库存事务，外部商品不允许本地下单 | 写入一致性及重复取消回补控制 | 确认靠对话，不等于真实支付；持久化错误不能吞掉 | 可作替代亮点，但偏后端领域，当前只作状态证据 |

表中 application/、infrastructure/ 均位于 app/。完整机器文件目录、测试名、源码哈希、当前安全配置将保存于 benchmarks/aggregates/audit_inventory.json。

## 旧 benchmark 的关键有效性问题

1. 原配置 protection off/on 的12/39由权限等瓶颈主导，授权补充36/39仍无主动故障注入；这不是保护机制无效的因果结论。两组都必须保留，不能合并成78个独立任务。
2. off 同时移除 Harness、输出过滤、模型重试、fallback、工具resilience、turn retry。只能评价整包配置；本机 fallback 主备同名，开组也没有真实备用模型。
3. 旧 compression 两组都挂 sidecar，且只对压缩组调用compress；4/8/16轮构造历史、末端单次事实探针，不能说明15–30轮全程累计成本。强制缩小window必须披露，默认128k触发未测。
4. 旧 isolation 的 demands 把需求全部重述，并明确旧要求作废；自然会低估“父代理漏传约束”的失败。新增明确的遗漏需求桶，不将省token误称正确性提升。
5. 旧 faults 含受控外部桩，不是业务流量；tool_timeout无保护等待后可成功，有保护主动超时失败，混合总体通过率会惩罚合理的截止时间策略。新增同一deadline和安全失败指标。
6. 旧retrieval67题是已知种子库开发集，重复3次仍只有67个query；无独立人工标注。现路径rules vs rules+dense可比，不应改名BM25/dense/hybrid。
7. 旧并发脚本不同query不可比；后续配对相同demands较好，但上下文和权限环境必须一致，不能只统计成功延迟。
8. cache的86.86%与61.88%是cached tokens / input tokens；首轮不是可控冷缓存，后续重复收敛。不可称request hit rate或system-only缓存比例。
9. shopping-v1为AI编写与AI复核的非独立需求验收；末题联网预算耗尽仍返回正常文本，不能把transport_success当网页刷新成功。原记录保留；本次不恢复被用户中止的补测任务。
10. 旧人工judge缺schema可把空rubric、字符串false算通过；新评分器必须严格类型与完整覆盖。历史金额格式复核保留原分数，但它是post-hoc更正，不作为本次预登记评分。

## 最多五个候选亮点与优先级

评分1–5为工程判断，不是实验数据；教程重合度越高越常见。

| 工程问题 → 设计及代价 | 深度 | 价值 | 可测性 | 面试区分 | 教程重合 | 完整度 |
|---|---:|---:|---:|---:|---:|---:|
| 瞬时恢复与持续失败/部分流安全的冲突 → 分层retry、首chunk边界、breaker；代价为尾延迟和状态复杂度 | 5 | 5 | 5 | 5 | 3 | 3 |
| 长历史成本与精确事实保留的冲突 → recent/summary/原有sidecar；代价为摘要调用与旧实体膨胀 | 5 | 5 | 5 | 5 | 3 | 3 |
| 专家上下文冗余与必要信息遗漏的冲突 → 自包含demands+服务端偏好；代价为父代理的信息选择责任 | 4 | 4 | 5 | 4 | 3 | 3 |
| 语义相关但无法购买的候选 → 约束、价格证据和unknown准入；代价为拒绝率与召回损失 | 4 | 5 | 4 | 5 | 2 | 3 |
| 事件很多却无法追因 → OTEL父子关系+业务事件；代价为导出成本与错误语义不一致 | 4 | 4 | 5 | 4 | 4 | 3 |

先选前三项做最小因果实验，选择依据是可控制变量且能暴露负结果，不是因为它们叫Multi-Agent/Memory/Reliability。约束准入排第四，后续优先于泛泛扩展路由策略；可观测作为横向证据检查。具体预登记见 benchmark_engineering_plan.md。
