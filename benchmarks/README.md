# 简历对应的上下文对照实验

本轮只执行 `context_resume`。早期 `prepare.py` 及宽范围工程计划尚未执行，不应把它们算作已完成实验。

在仓库根目录使用现有 `.venv`。密钥仅由已有 `.env` 载入，不进入结果文件。重跑真实API会产生费用，必须使用新目录名，原始记录不覆盖。

```powershell
# 仅验证现有冻结数据的上下文长度，无付费调用
.\.venv\Scripts\python.exe -B -m benchmarks.runners.context_resume --name preflight --preflight

# 真实运行12题×3组，估算高峰费用停止线3元
.\.venv\Scripts\python.exe -B -m benchmarks.runners.context_resume --name context-resume-new

# 仅重算当前正式实验的报告，不调用API
.\.venv\Scripts\python.exe -B -m benchmarks.runners.context_report --run benchmarks/raw/context-resume-v1

# 评分器与采集器契约回归
.\.venv\Scripts\python.exe -B -m pytest tests/test_context_resume_benchmark.py tests/test_benchmark.py -q -p no:cacheprovider
```

- `datasets/context_resume_v1.json`：正式12个合成轨迹，历史15/20/24轮加6次继续输入；固定逐轮gold。
- `datasets/context_resume_pilot.json`：独立接口pilot，不混入正式样本。
- `raw/context-resume-v1/source/`：测量时源码、数据与协议快照。
- `raw_results.jsonl`：36组案例结果；一组成功要求6次状态探针全部通过。
- `turns.jsonl` / `turns.csv`：216次探针、逐字段判定、含摘要的分轮成本。
- `model_calls.jsonl`：逐请求provider usage、phase、实际响应model、错误。
- `requests.jsonl` / `artifacts/request-*.json`：真实模型请求内容和SHA256，不保存认证headers。
- `artifacts/<case>-<arm>-<turn>.json`：答案、预登记gold、summary、原有事实账本及上下文。
- `context_summary.json`：累计Token与费用、配对收支平衡点、全部失败。
- `spending.json`：含pilot的高峰价费用估算和停止线；不是平台扣款账单。
- `verification.json`：快照哈希、请求配置、样本覆盖及分数重算检查，不等于独立人工审核。

解释以 `docs/context_benchmark_protocol.md` 与 `docs/context_benchmark_results.md` 为准。状态探针不等于购物完成率；回放历史不等于真实用户会话；受控4096窗口不等于部署128k。API缓存不可人为清空，成本与延迟不作生产泛化。
