# Benchmark 文档与数据集审计

审计日期：2026-10-02。配套方案见 [benchmark-methodology.md](benchmark-methodology.md)。本审计先于正式计分实验，目标是识别可复用样本、无效成功判据与历史证据边界，不以简历原数字为目标。本文件是研究产物，不是业务修复或 benchmark 成绩报告。行号以本次读取源码为准，后续变更应结合运行 manifest 的文件哈希核验。

## 阅读覆盖与方法

完整阅读以下第一方材料：

- 根目录 `README.md`、`.env.example`、`pyproject.toml`、`docker/docker-compose.yaml`、`app/infrastructure/settings.py`。
- 审计开始时 `docs/` 全部 5 篇：`教程实现对齐清单.md`、`设计演进记录.md`、`面试案例-商品检索向量建库与静默降级.md`、`网页知识入库.md`、`product_recommendation_demo.md`。
- `output/interview-prep/README.md`、`01-项目介绍与架构.md`、`02-简历对照与代码索引.md`、`03-编排与记忆问答.md`、`04-执行保护与观测问答.md`、`05-检索与设计取舍.md`、`06-故障推演与复习清单.md`、`07-reliability-repair-report.md`、`08-order-queue-fixes.md`、`source-snapshot.json`。
- `eval/cases.yaml`、`eval/product_recall.jsonl`、`eval/category_recall.jsonl`；以及 `scripts/eval_regression.py`、`scripts/eval/run_product_recall.py`、`scripts/eval/metrics.py`、`scripts/eval/validate_datasets.py`。

对实际装配、种子语料、当前推荐锁、队列字段和追踪中间件做了定向源码核对。核心 Agent、工具、存储、故障机制与全部测试的完整审阅由协调任务负责；本文件不冒称独立覆盖全部业务代码。没有读取真实 `.env`、输出凭据、调用模型、执行交易或修改业务文件。本次只读验证使用 `python -B`，没有把历史测试数字当作本次测试成绩。

## 文档时效与当前执行链

| 发现 | 第一方证据 | 对实验的影响 |
|---|---|---|
| README 的商品向量召回加 rerank 主链描述已过时 | `README.md:8,63`；当前 `app/composition.py:231` 装配 `ProductRecommendationService` | 主评测必须走当前推荐服务。旧 `CatalogSearchUseCase` 实验单独标为旧模块，不能称当前完整流程 |
| 旧网页知识入库不是当前商品发现流程 | `README.md:70`；`app/composition.py:174` 的 `web_knowledge_service=None`；`docs/product_recommendation_demo.md:56` | 网页知识文档适合作为历史设计，不据此宣称当前 Agent 自动执行那条链 |
| 面试材料 01–06 是早期静态快照，07、08 后续记录修复 | `output/interview-prep/README.md:5`、`source-snapshot.json`；`07-reliability-repair-report.md:5–10`、`08-order-queue-fixes.md:5–13` | 旧文档的全局推荐锁、偏好撤回不注入、半开多探针、缺幂等、缺 pending 回收、缺 traceparent 不能直接当现状 |
| 当前确实已有请求级锁和队列关联字段 | `app/application/usecases/product_recommendation.py:105–117`；`app/domain/queue/ports/task_queue.py:36–37,49–50,64–65`；`app/infrastructure/queue/redis_stream_queue.py:225` | 新并发/追踪实验应测当前机制，而非按旧缺口设计必然获胜的对照 |
| 关键事实中间件已装配 | `app/infrastructure/tracing.py:52–53` | 压缩 token 成本应包含中间件加入的事实账本，不只量摘要长度 |
| 历史设计记录内部有矛盾 | `docs/设计演进记录.md:20` 称 MySQL 实现，`:79` 明确不做 MySQL | 以当前驱动、仓储和实际运行配置为准，不将文字当部署证据 |
| 测试数量来自不同日期 | `README.md:137` 为137；`docs/product_recommendation_demo.md:56` 为418；`output/interview-prep/07-reliability-repair-report.md:14` 为442 | 这些不能合并、累加或称本次全量通过；本次成绩由执行输出证明 |

`SearchAgentFactory` 当前商品工具仍标为 `is_read_only=False`（`app/application/agents/search_agent.py:68`）。这可能影响框架权限流程，真实 benchmark 必须保存许可挂起等结果，不通过临时放宽业务权限使实验通过。静态风险本身不等于已复现故障。

## 13 类对话样本与可验证成功标准

现有 `eval/cases.yaml` 是固定回归样本，共13类，不是独立采样的真实用户任务。可以复用业务意图，但应将事实、行为与表达分开判定。模型说做过不等于工具或仓储已执行。

| Case / 来源行 | 现有目标 | 新计分需要的证据或修订 |
|---|---|---|
| search-budget / 6 | 预算内露营灯 | 实际商品工具调用；spec 币种与价格上限正确；推荐商品确实存在且符合品类和预算。不能仅因包含 LumenGo 名称通过 |
| landed-price-us / 19 | 美国到手价明细 | 实际工具中的原价、运费、税费、总额与币种；分项总和正确；未知费用不能当零。固定商品与演示规则只支撑该实验域 |
| compare-two / 33 | 两商品到手价比较 | 两件商品都有有效工具证据，金额、币种、到手价与结论对应；不得只从文本含两个名字推断完成 |
| order-confirm-card / 45 | 确认前不下单 | 第一轮后仓储零新增订单、库存未扣，同时输出必要确认信息；原“回复无 GBX-”不能排除后台已下单 |
| order-full-cycle / 58 | 确认、创建、取消 | 第二轮恰好一个正确订单并扣一次库存，第三轮 CANCELLED 并回补一次；目前查询序列没有独立查单轮次，不能按描述称完整查单验收 |
| memory-write / 74 | 保存长期偏好 | 记忆工具实际调用和该 buyer 的 store 写入；最终确认内容正确；不能只认“已记住” |
| memory-recall / 87 | 跨会话偏好 | 独立隔离的前置写入并核验；新 session 同 buyer 的读回、输入和输出证据；若商品材质证据未知，按当前契约标未知或澄清，不硬塞旧预期推荐 |
| no-fabrication / 106 | 无无人机时不编造 | 关闭可选联网并声明固定本地目录，或验证外部商品的真实证据。开放 Tavily 时“种子无无人机”不等于没有真实候选 |
| order-not-found / 118 | 查不存在订单 | 工具实际返回 not-found，最终不捏造订单状态；使用实验隔离库 |
| chitchat-boundary / 130 | 闲聊不调工具 | 保存实际工具调用计数并断言为零；原 rubric 只查自我介绍和表达，未查标题要求 |
| category-insight / 142 | RAG 选购知识 | 实际知识工具与来源证据支持关键事实；遵守不推商品；自然表达可另作人工项目 |
| long-context-memory / 155 | 多轮关键事实 | 原5轮只可测回忆，默认 `CONTEXT_SIZE=128000`（settings.py:140）不能保证触发压缩；压缩实验须记录真实压缩发生，并核验商品ID、金额、预算、待确认状态 |
| tool-error-honesty / 172 | 巴西不支持时如实报告 | 实际不支持/无法计算证据和无虚构运税；这是业务边界样本，不替代429、超时、流中失败、熔断实验 |

任务成功建议使用所有预登记必要断言的合取，保存每个断言、错误与原始输出。对拒绝或降级，分别记录“购物任务完成”“正确报告不能完成”“传输正常”，不能都汇入同一完成率。表达质量、答案整体合理性仍需要独立人工或严格校准的 judge，不能把自动可核验部分冒充全部质量。

## 现有 LLM judge 的确定缺陷

来源均为 `scripts/eval_regression.py`：

1. `score_case`（124–131）把空评分档位计满分，并按 Python truthiness 判 `pass`，不验证严格 boolean。只读执行证实 `score_case({}) == (1.0, True)`，以及字符串 `"false"` 也被算通过。新评测必须拒绝缺失/重复 rubric、类型错误和不完整 judge 输出。
2. 最终 PASS 条件为 P0 全过、加权分不低于0.7（162）。这允许部分必要 P1 行为失败，不能直接等同任务完成。
3. judge 输入只有对话文本（151–155），没有实际工具调用或仓储证据，因此不能判断下单、副作用、记忆写入、零工具调用等行为。
4. 每次随机 session，但 buyer 固定（135–136）。跨 run 的偏好可能污染结果；`memory-recall` 又无条件把 prior_context 声明为真实，即使前一个 case 没有成功写入。新样本需独立前置核验和隔离 owner。
5. `build_ground_truth`（54–72）仅用种子商品与固定汇率。动态库存、外部目录和工具实际费用不在该事实表；judge 系统提示（40–47）仅要求衍生运税自洽，不能确保确实来自工具。
6. 语义缓存预检（197–216）值得保留，但读取 health 失败会跳过；报告为 Markdown（254–256），缺少逐请求原始 usage、延迟与可重聚合的 JSONL。

上述缺陷不证明历史每条评分都错，但使历史总分不足以严格支持简历的任务完成率。

## 召回标注的用途和偏差

`eval/product_recall.jsonl` 的前55条是 lexical，后12条是 semantic；`eval/category_recall.jsonl` 有22条。商品 relevant 标 product_id，品类标知识文档名。全部保留作当前固定集，不根据结果删除失败例或改答案。

- **语料设计偏向**：`app/infrastructure/persistence/seed_products.py:5` 明示标题和描述关键词化；`:9` 保留前10个SPU以维持测试排序；`:16` 明示新增语料避开 P1001 的已有查询竞争。因此不是从真实商品与用户流量独立抽样的无偏集。
- **样本相关性**：semantic 查询常是已有 lexical 品类的需求改述，例如颈枕、眼罩、行李秤；重复运行只增加同case随机性观测，不增加独立用户需求种类。应分别报查询数与运行数，并按case/查询家族考虑不确定性。
- **标注一致性未知**：历史文档明确单人标注（`docs/教程实现对齐清单.md:276`），无独立标注或仲裁记录。结构自检通过不等于 relevance 完整、无争议。
- **排序不是独立等级标签**：`scripts/eval/metrics.py:64–76` 按 relevant 顺序给线性 gain。NDCG 的含义依赖标注列表顺序，不能误称使用了独立分级相关性。
- **过滤口径不足**：`scripts/eval/run_product_recall.py:103–136` 只查不可配送泄漏及 relevant 被列入 filtered_out；没有独立核验价格超限。新报告不要复用笼统的“硬约束过滤准确率100%”。
- **策略名称不等于执行策略**：旧脚本 `:271–273` 接收却不使用 actual，以请求 strategy 标报告；逐条结果未保存真实 recall_strategy/rerank_applied。向量失败降级和没配 reranker 必须在新原始结果中明确，不能算目标策略有效样本。
- **旧门禁是总体绝对下限**：`scripts/eval/metrics.py:142–159` 不检查子组、过滤正确率或历史差值。67条中55条 lexical 可掩盖12条 semantic 的显著退化；PASS 不是“无退化”。
- **旧模块不代表端到端**：`scripts/eval/run_product_recall.py:3–5,141–149` 直接构造 spec 调旧 UseCase，绕过自然语言参数提取、Agent 与HTTP。当前规则服务与旧标签不一致时单列原因，不能为了高分调整标注。

只读自检命令：

```powershell
.venv/Scripts/python.exe -B scripts/eval/validate_datasets.py
.venv/Scripts/python.exe -B -c "from scripts.eval_regression import score_case; print(score_case({})); print(score_case({'p0':[{'pass':'false'}],'p1':[{'pass':True}],'p2':[{'pass':True}]}))"
```

本次自检输出商品67条、品类22条均通过；两次评分反例输出均为 `(1.0, True)`。没有执行需要真实模型的旧回归脚本。

## 旧数字与简历支持范围

| 旧数字或能力 | 来源 | 可引用范围 |
|---|---|---|
| Prompt Cache 80%、任务完成71%→89%、定位30→5分钟 | `output/interview-prep/README.md:25`、`02-简历对照与代码索引.md:20,24,26` | 均明确未验证，不能作实验预期。新跑测只能支持新版本/样本/模型的结果，不能追认历史提升 |
| 并行1.84×（20.6s对38.0s）、13/13和0.988 | `docs/设计演进记录.md:57,186` | 历史特定模型记录，缺当前配对重复原始样本；新 benchmark 不能沿用这个基线 |
| 语义缓存8.8s→3.0s | `docs/设计演进记录.md:69` | 业务答案缓存，不是供应商 Prompt Cache，更不是稳定命中率 |
| 商品Recall@8 0.978/0.871、语义1.000/0.361 | `docs/教程实现对齐清单.md:255–276` | 旧60-SPU、67标签、12语义查询的模块实验；未配置reranker，不能证明重排收益，也不是此次修复前后对照 |
| 网页发现、缓存、持久化验收 | `docs/product_recommendation_demo.md:28–38` | 明确为工具/服务端到端，未调用完整LLM Agent；服务调用次数不等于Tavily计费请求次数 |
| GPU、vLLM、P50/P99、预热参考 | `docs/面试案例-商品检索向量建库与静默降级.md:366–474` | 截图参考材料，部署归属和同口径原始trace未核验；不同阶段/基线不能合并，分位数不能相加 |

人的问题定位时间需要参与者实际开始、提交根因、正确性验收及盲测/次序控制。自动脚本耗时、错误事件出现延迟、trace覆盖率都是不同指标。没有人工样本时该指标应是 unavailable/null，不能用程序生成30或5分钟。

## 对新方案的约束

新任务集应冻结输入、前置状态、断言和配置后再开始计分；相同case使用配对、交错组别，记录失败、缺失字段、实际路径及调用成本。不把已知开发集包装成 held-out；若新增样本，要明确是预先编写的合成回归场景，不称真实用户抽样。第三方收费/不可控故障可在边界模拟并逐行标注，不能把注入故障收益当自然流量完成率。

指标报告需保留模型/依赖版本、文件哈希、独立case数、重复次数、总体与分层统计及其不确定性。业务缺陷导致低分应原样保留；benchmark自身的无效断言修正必须有版本和pilot记录，不能静默筛选出获胜方案。
