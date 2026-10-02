# 重跑与审阅 benchmark

在仓库根目录 PowerShell 执行。环境沿用 `.venv` 与本机 `.env`，真实 API suite 复用 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`，不输出密钥。不要把 `.env` 放入报告。

```powershell
# 本地真实 BGE / SQLite / 当前推荐链路 + 明确标注的外部故障注入
.\.venv\Scripts\python.exe -m scripts.benchmark.run --suite local --output output/benchmarks/local-new

# 原配置真实 Agent，缓存用量，专家派发
.\.venv\Scripts\python.exe -m scripts.benchmark.run --suite agent,parallel,cache --output output/benchmarks/live-default-new

# 独立补充实验：在测试环境预授权本地商品搜索，业务源码不变
.\.venv\Scripts\python.exe -m scripts.benchmark.run --suite compression,isolation,agent,parallel --consent-product-search --output output/benchmarks/live-consented-new

# 仅重算统计，不调用模型，不改原始记录
.\.venv\Scripts\python.exe -m scripts.benchmark.aggregate output/benchmarks/local-new

# 对旧评分器产生的完整Agent运行，复核等价金额格式（保留原始分数）
.\.venv\Scripts\python.exe -m scripts.benchmark.rescore output/benchmarks/live-default-v1
.\.venv\Scripts\python.exe -m scripts.benchmark.rescore output/benchmarks/live-consented-v3

# 审核样本数、重复行、artifact、源码快照与usage合法性
.\.venv\Scripts\python.exe -m scripts.benchmark.verify output/benchmarks/local-new

# 使用本轮正式原始记录重建中文报告，不调用模型
.\.venv\Scripts\python.exe -m scripts.benchmark.report --local output/benchmarks/local-v1 --default output/benchmarks/live-default-v1 --consented output/benchmarks/live-consented-v3 --output docs/benchmark-results.md

# 采集器的独立回归检查
.\.venv\Scripts\python.exe -m pytest tests/test_benchmark.py -q -p no:cacheprovider
```

输出路径必须是新建或空目录，避免覆盖旧实验。`--limit-cases 1 --repeats 1 --compression-repeats 1` 是采集器pilot，manifest标记pilot，不能与正式样本混算。若达到 `--max-calls`（默认800），未完成任务不会被伪装成成功。默认单任务180秒；模型调用不并行跑不同实验，以免引入额外服务端竞争。

## 结果文件

- `manifest.json`：执行状态、参数、依赖、系统、输入和源码SHA256。
- `source/`：与manifest哈希相符的测量时源码/数据副本；不含密钥、模型权重或依赖二进制。
- `safe-settings.json`：真实API模型与主机、可公开实验配置。
- `effective-settings.json`（补充实验）：脱敏后的基础设置，另结合冻结源码中逐实验的覆盖项。
- `*-cases.json`：在该suite首个计分请求之前固定的数据。
- `results.jsonl`：每任务结果；失败也保留。`results.csv`是可重新生成的平面表。
- `model_calls.jsonl`：原始provider usage、实际返回模型、请求哈希、时延与错误。缓存字段缺失为null。
- `turns.jsonl`、`spans.jsonl`、`artifacts/`：回答、真实事件、状态、SQL结果及Trace；均为合成测试数据。
- `state/`：隔离的SQLite、商品缓存和Qdrant；不会读取现有用户会话。
- `aggregate.json/md`：统计、分层、配对比较、用量及事件与导出Trace的实际关联。
- `aggregation-manifest.json`：此次汇总器版本与所使用原始JSONL哈希。
- `aggregation-source/`：实际汇总器与通用辅助代码副本，避免运行后的评分修正只剩哈希而无法检查源码。
- `verification.json`：完整性审核；valid=true仅说明实验记录完整，不代表业务成功。
- `score-review.jsonl`、`score-review-manifest.json`、`score-review-evaluator.py`：格式误判复核的逐条新旧评分、证据哈希和评分器；存在有效复核记录时聚合采用新评分，原始JSONL仍保留原分数。
- `execution-notes.json`（补充实验）：回归测试重叠等运行限制及受影响样本，不从统计中删掉这些观测。

正式结果的解释以 [benchmark-methodology.md](benchmark-methodology.md) 为准。默认组与授权补充组的配置不同，不能合并成功率或把它们解释成一次历史修复的前后对照。

## 人工问题定位时间

本轮没有真人盲测，因此不得写“30分钟缩短到5分钟”。真人参与时，先冻结故障集并分配日志/Trace条件，不让同一参与者提前知道同一问题答案。实际开始和完成时执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.benchmark.diagnosis start --output output/benchmarks/human-study --participant reviewer01 --case fault01 --condition logs
.\.venv\Scripts\python.exe -m scripts.benchmark.diagnosis finish --output output/benchmarks/human-study --participant reviewer01 --case fault01 --root-cause "参与者实际确认的根因" --correct unreviewed
```

`--correct yes/no`需要独立审核人核对真实根因，不能自行假定正确；未审核结果不纳入正确诊断耗时。JSONL保留原始事件，采集器只计实际经过的墙钟时间。暂停、熟悉效应、参与者经验和故障难度均是需要另外控制的混杂因素。
