# Engineering benchmark v1 — preregistration

2026-10-02，在本轮实验开始前冻结。所有题目由Codex构造，均为exploratory开发评估；不是人工标注、真实用户分布或独立holdout。旧数据只作单独的再分析，绝不替换原始失败。

## 先执行的最小实验

1. **Context**：3个合成轨迹，分别15/20/30轮，固定购物事实与无关插话，部分轨迹含预算/目的地更新。4组：no compression、最近4轮滑窗（新增benchmark baseline）、原有summary+recent但移除sidecar、原有summary+recent+CriticalFactsMiddleware。同一题四组逐轮输入完全相同。对每个历史轮执行一次真实模型事实探针，历史助手回复使用冻结的构造文本，不把它称为自主购物对话。探针回复不写回历史，避免上一次测验提示答案；累计成本覆盖所有探针与摘要。固定受控context_size=4096，trigger=.75，reserve=.15；这不是部署128k。保存每轮实际输入、输出、summary、sidecar、真实usage。探针输出严格JSON七字段：budget/country/excluded_brand/excluded_type/product_id/pending_action/quantity；缺失/错类型/null不作正确。没有商品工具执行，不宣称完成真实购物或下单。最终全部七项正确为case success，逐轮字段正确/总字段为retention。
2. **Task context**：8题，2种商品×4种历史（无干扰、无关插话、已撤销约束、demands漏掉有效预算/目的地）。同一parent历史和demands，两组分别继承全部parent history/仅demands，实际SearchAgentFactory和真实本地商品服务；当前用户事实来自冻结parent历史，gold不注入专家。每题一次，不把重复当独立。商品查询统一预授权，两组相同；无外部联网。评价实际调用的预算/地区、候选金额、最终引用商品ID；没有调用、权限拒绝、工具/API错误全部进分母。直接调用专家用于隔离机制，不声称测过父代理拆解质量或完整routing。
3. **Faults**：外部API边界可控替身，内部仍用真实ThrottledChatModel/ToolResilienceMiddleware/HarnessToolMiddleware。模型故障normal、once429、persistent503、invalid400、empty-message timeout、pre-stream、partial-stream，比较baseline/retry/fallback/full；工具持续失败比较breaker off/on，阈值3、reset .03秒；6次失败后半开成功；相同外部行为、固定调度。循环与schema试验直接调用middleware，明确这种接线并非所有生产工具都具备。重复3次只估测运行波动；不产生虚构LLM token节省。恢复、废请求、安全不重播、回退、半开分别计数；失败率不掩盖主动超时保护。

固定seed=20261002，只随机化配对顺序；temperature=0、禁用SDK隐式重试、记录实际返回model；provider确定性seed不受控须披露。上下文与专家使用现有配置模型，最大真实请求400次、单任务180秒、每次API90秒。运行前保存输入、评分逻辑、脚本、协议与app源码哈希；旧付费调用授权沿用，本轮不更换账号、不购买配额。当前主备同名导致无真实fallback，故故障注入中的独立备用替身只能证明机制。

## 判断和聚合

所有run保留manifest/config/raw_results/model_calls/artifacts/aggregate；每观测键为suite+case_id+variant+repeat(+turn)。记录git不可用、dirty未知、文件hash、环境、时间、model、配置、seed、dataset hash。成功/部分/失败先按固定字段判断，严格包含超时/permission/API/quality/measurement failure；缺usage为null。分组给N、独立case数、成功/总数、失败类别，全部尝试mean/median/P90。比率取明确分子分母，token累计含summary，不能只拿最后一轮。字段NA不补零，尤其没有真实模型的故障试验不报虚构token。

不会为追求显著性大量重跑/扫参；本轮无统计显著性用语。结果驱动的后续实验必须另存计划与目录。若默认窗口未触发，不得修改阈值后把受控结果混入默认结果。

## 后续决策规则

- 若task-specific只省token且漏传约束更差，报告trade-off，不继续用always-multi强造收益。
- 若summary+sidecar不优于summary-only，保留负结果；通过现有实体账本检查解释开销，不修改业务代码救分。
- 若faults机制有分层差异，补充trace关联和现有default/consented失败类型再分析；不宣称旧71→89。
- 若检索发现“名词与实际路径不符”，先再分析当前rules/dense与约束失败，不实现虚假的BM25对照。
- cache仅再分析真实usage并按可获得的对话长度分层；没有5/10/20/30真实会话标签就明确NA，不伪造长度或system-only分母。

所有新代码限定benchmarks/、tests/与实验文档；不为本轮分数修改app。若遇到采集器bug，保留失败pilot，新目录重跑；业务bug只记录因果证据与修复建议。
