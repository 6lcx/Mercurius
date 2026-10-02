# 上下文压缩与事实保留：简历对应实测

本轮只验证现有上下文机制，业务代码未修改。12个合成轨迹×3组，每组72次真实后续状态探针，另计摘要请求。历史回放由Codex构造，不是21–30轮自主Agent购物对话。

## 三组结果

| 方案 | 六轮全对案例 | 全对探针 | 字段正确 | 累计输入Token（含摘要） | 累计输出Token | 模型请求 | 摘要请求 | 高峰价估算元 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 完整历史 | 12/12 | 72/72 | 648/648 | 288,744 | 5,399 | 72 | 0 | 0.1847 |
| 摘要＋近期 | 12/12 | 72/72 | 648/648 | 141,052 | 13,259 | 84 | 12 | 0.2625 |
| 摘要＋近期＋事实侧账本 | 12/12 | 72/72 | 648/648 | 150,248 | 13,638 | 84 | 12 | 0.3060 |

摘要＋近期相对完整历史：本轮累计输入变化-51.15%，高峰价估算变化+42.15%。输入缩减与费用变化是不同指标，费用受缓存影响。

摘要＋近期＋事实侧账本相对完整历史：本轮累计输入变化-47.96%，高峰价估算变化+65.71%。输入缩减与费用变化是不同指标，费用受缓存影响。

## 对简历这一条的具体意义

- **已经支持的收益**：在固定合成历史与6次后续探针中，摘要＋近期消息的累计输入为141,052，完整历史为288,744；摘要成本已计入。事实更新探针通过72/72，没有观察到本轮正确性损失。
- **未支持的说法**：不能据此说压缩一定省钱或加速。完整历史在多次请求间复用缓存，摘要额外生成了输出；本轮费用与总耗时的实际方向见表。窗口空间、累计输入、计费和延迟是不同目标。
- **事实侧账本的增量**：相对仅摘要，它增加9,196输入Token，整题通过数为12对12。这批用例没有证明额外正确性收益，也不能证明机制在更长或更复杂的任务中无用。
- **实现上的解释**：原有侧账本从工具调用和返回值保留ID、金额等历史证据，并动态附加system。它不是完整的当前意图状态机；用户修改与旧事实的优先级仍需要模型遵守。
- **面试应讲的取舍**：历史较长时用摘要控制输入规模；是否节省费用要连同摘要输出、后续轮数和provider缓存计算。不要把“输入少了”直接解释为“费用和耗时都下降”。

## 输入、摘要与缓存成本拆分

| 方案 | 每次探针平均输入Token | 摘要输入Token | 摘要输出Token | 缓存输入/全部输入Token | 缓存Token占比 |
|---|---:|---:|---:|---:|---:|
| 完整历史 | 4010.3 | 0 | 0 | 222,464/288,744 | 77.05% |
| 摘要＋近期 | 1254.5 | 50,728 | 8,048 | 64,128/141,052 | 45.46% |
| 摘要＋近期＋事实侧账本 | 1307.9 | 56,080 | 7,763 | 52,864/150,248 | 35.18% |

缓存Token占比不是请求命中率；此处全部输入同时包含摘要与探针，不称system-only命中率。

## 摘要开销何时摊销

以下每个点累计了12题相同数量的后续探针及此前所有摘要。达到输入Token收支平衡不等于达到费用收支平衡；缓存无法清空，价格按高峰统一估算。

| 后续探针数 | 完整历史累计输入 | 摘要累计输入 | 摘要＋事实累计输入 | 完整历史估算元 | 摘要估算元 | 摘要＋事实估算元 |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 47,216 | 64,874 | 70,852 | 0.1020 | 0.1922 | 0.2084 |
| 2 | 94,840 | 79,428 | 86,104 | 0.1188 | 0.2066 | 0.2414 |
| 3 | 142,800 | 94,318 | 101,620 | 0.1353 | 0.2212 | 0.2583 |
| 4 | 191,112 | 109,560 | 117,504 | 0.1525 | 0.2351 | 0.2764 |
| 5 | 239,760 | 125,138 | 133,708 | 0.1686 | 0.2493 | 0.2913 |
| 6 | 288,744 | 141,052 | 150,248 | 0.1847 | 0.2625 | 0.3060 |

逐题首次收支平衡（后续探针序号；“未达到”只表示本轮6次观测内未达到，并非永远不会）：

| 用例 | 方案 | 输入平衡点 | 估算费用平衡点 |
|---|---|---:|---:|
| stable_lamp | 摘要＋近期 | 2 | 未达到 |
| stable_lamp | 摘要＋近期＋事实侧账本 | 3 | 未达到 |
| stable_charger | 摘要＋近期 | 2 | 未达到 |
| stable_charger | 摘要＋近期＋事实侧账本 | 2 | 未达到 |
| stable_backpack | 摘要＋近期 | 2 | 未达到 |
| stable_backpack | 摘要＋近期＋事实侧账本 | 2 | 未达到 |
| budget_replace | 摘要＋近期 | 2 | 未达到 |
| budget_replace | 摘要＋近期＋事实侧账本 | 3 | 未达到 |
| country_replace | 摘要＋近期 | 2 | 未达到 |
| country_replace | 摘要＋近期＋事实侧账本 | 2 | 未达到 |
| product_replace | 摘要＋近期 | 2 | 未达到 |
| product_replace | 摘要＋近期＋事实侧账本 | 2 | 未达到 |
| brand_withdraw | 摘要＋近期 | 2 | 未达到 |
| brand_withdraw | 摘要＋近期＋事实侧账本 | 3 | 未达到 |
| type_withdraw | 摘要＋近期 | 2 | 未达到 |
| type_withdraw | 摘要＋近期＋事实侧账本 | 2 | 未达到 |
| temporary_brand | 摘要＋近期 | 2 | 未达到 |
| temporary_brand | 摘要＋近期＋事实侧账本 | 2 | 未达到 |
| intent_withdraw | 摘要＋近期 | 2 | 未达到 |
| intent_withdraw | 摘要＋近期＋事实侧账本 | 3 | 未达到 |
| multiple_revision | 摘要＋近期 | 2 | 未达到 |
| multiple_revision | 摘要＋近期＋事实侧账本 | 2 | 未达到 |
| distractor_numbers | 摘要＋近期 | 2 | 未达到 |
| distractor_numbers | 摘要＋近期＋事实侧账本 | 2 | 未达到 |

## 耗时及错误

| 方案 | 每题六轮含摘要 mean/median/P90秒 | 错误请求 | usage缺失 |
|---|---|---:|---:|
| 完整历史 | 5.305/5.136/6.002 | 0 | 0 |
| 摘要＋近期 | 8.701/8.656/9.492 | 0 | 0 |
| 摘要＋近期＋事实侧账本 | 8.871/8.877/9.454 | 0 | 0 |

失败全量列出，不为通过率删除；JSON字段名/标题精确匹配仍比开放式推荐严格，不能把这项状态探针当全面自然语言质量。

本轮没有未通过探针。该结果不证明任何长度/任何用户任务都不会丢失事实。

## 解释与边界

- full_history与summary_recent比较摘要机制；summary_recent与summary_facts比较现有事实侧账本增量。不能将所有摘要收益归于侧账本。
- 同一题的6轮不是6个独立案例，每组独立设计case数12；未做显著性检验，不声称显著提升或生产泛化。
- 使用受控4096窗口触发现有压缩；默认128k的触发频率未测。历史为冻结回放，探针输出不回填，以防连续测验泄漏答案；没有业务工具和真实订单。
- 所有组thinking disabled、temperature=0、禁用隐式重试；保留配置模型别名及provider返回模型，不能与之前thinking配置直接拼接。
- 单次执行和共享provider缓存可能影响费用与耗时；seed控制执行顺序，不保证provider完全确定。
- 费用按[DeepSeek官方高峰单价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)计算，是保守估算而非账单；低峰价可能更低。无usage请求按预留额计算，不假定免费。
- pilot保留原始词面误判，正式前固定refurbished等价词；正式开始后未修改题目、成功标准或业务源码。

## 复现与证据

- [预登记协议](C:/Users/20685/Desktop/mercurius-agent-main/benchmarks/raw/context-resume-v1/source/docs/context_benchmark_protocol.md)
- [冻结数据](C:/Users/20685/Desktop/mercurius-agent-main/benchmarks/raw/context-resume-v1/source/benchmarks/datasets/context_resume_v1.json)
- [逐题原始结果](C:/Users/20685/Desktop/mercurius-agent-main/benchmarks/raw/context-resume-v1/raw_results.jsonl)
- [逐轮CSV](C:/Users/20685/Desktop/mercurius-agent-main/benchmarks/raw/context-resume-v1/turns.csv)
- [真实模型usage](C:/Users/20685/Desktop/mercurius-agent-main/benchmarks/raw/context-resume-v1/model_calls.jsonl)
- [全部请求与输入](C:/Users/20685/Desktop/mercurius-agent-main/benchmarks/raw/context-resume-v1/requests.jsonl)
- [校验记录](C:/Users/20685/Desktop/mercurius-agent-main/benchmarks/raw/context-resume-v1/verification.json)

重跑：`python -m benchmarks.runners.context_resume --name 新目录名 --prior-cost 0`；费用停止线3元。仅重建报告：`python -m benchmarks.runners.context_report --run benchmarks/raw/context-resume-v1`。

相关回归：20通过，1跳过，0失败，0错误。[JUnit记录](C:/Users/20685/Desktop/mercurius-agent-main/benchmarks/raw/context-resume-v1/validation-tests.xml)。该回归在付费测量全部完成后执行，没有与测量重叠。
