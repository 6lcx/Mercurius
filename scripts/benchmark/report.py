"""Generate a bounded interpretation from completed, verified run artifacts."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .aggregate import aggregate, load_rows, load_scored_rows
from .verify import verify
from .common import ROOT


def percent(x):
    return '未测得' if x is None else f'{100*x:.2f}%'


def number(x):
    return 'NA' if x is None else f'{x:.3f}'


def make_report(local, default, consented, destination):
    runs = [('本地', Path(local)), ('原配置', Path(default)), ('授权补充', Path(consented))]
    data = {}
    for name, path in runs:
        audit = verify(path)
        if not audit['valid']:
            raise ValueError(f'{name} is incomplete: {audit["errors"]}')
        manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('pilot'):
            raise ValueError('Pilot must not be used as formal evidence')
        data[name] = aggregate(path)
    group = lambda run, suite, variant: next(g for g in data[run]['groups'] if g['suite'] == suite and g['variant'] == variant)
    metric = lambda g, name: g['metrics'].get(name, {}).get('mean')
    cache = group('原配置', 'cache', 'stable_system_hint')['cache']
    cache_base = group('原配置', 'cache', 'dynamic_system_prefix')['cache']
    c0, c1 = group('授权补充', 'agent', 'protection_off'), group('授权补充', 'agent', 'protection_on')
    d0, d1 = group('原配置', 'agent', 'protection_off'), group('原配置', 'agent', 'protection_on')
    compression = group('授权补充', 'compression', 'project_compressed')
    isolated = group('授权补充', 'isolation', 'isolated_context')
    inherited = group('授权补充', 'isolation', 'inherited_history')
    memory_a, memory_b = group('本地', 'memory', 'all_preferences'), group('本地', 'memory', 'selected_preferences')
    ret_a, ret_b = group('本地', 'retrieval', 'rules'), group('本地', 'retrieval', 'rules_embedding')
    paths = {name: str(path.resolve()) for name, path in runs}
    lines = ['# Mercurius 简历 benchmark 实测报告', '',
        '测量日期：2026-10-02。业务 app/ 源码未修改；每次运行的 manifest、源码副本及完整性审核可核对。', '',
        '**结论：已有三个简历数字不能作为已复现的历史成果继续引用。** 本报告给出当前版本的新测量；失败、权限挂起和缺测没有删除或补零。', '',
        '## 方法与环境', '',
        '- 当前配置模型为 DeepSeek，实际返回模型名以 model_calls.jsonl.response_model 为准；本地 BGE-small-zh-v1.5 CPU embedding。使用项目 AgentScope、SQLite、Qdrant、工具与编排。',
        '- 固定种子商品和知识库；第三方网页发现、Redis语义缓存、队列关闭。订单是本地模拟订单，运税是项目估算，不是市场真实报价。',
        '- 现有任务/检索标注属于开发集，部分语料曾为既有单测构造；重复次数不等于独立样本数，不报告生产泛化。',
        '- 默认组保持现有权限；补充组仅在测试工厂中授权 product_search_tool。两种环境分开统计，不把两者当历史修复前后。',
        '- 真实API各实验顺序运行，AB/BA交错；temperature=0、输出上限4096（前缀机制实验512）；两组关闭SDK重试，保护组保留项目重试。模型服务仍有非确定性。',
        '- 耗时 mean/median/P90 为全部尝试的描述统计；成功任务耗时另外存于 aggregate.json。失败的快速返回不构成加速收益。',
        '- 完整 metric definition / baseline / treatment / dataset / sample size / success criteria / confounders 见 [方法计划](benchmark-methodology.md)。', '',
        '## 简历数字的判断', '',
        '| 原简历表述 | 本次证据 | 建议 |', '|---|---|---|',
        f'| Prompt Cache稳定80% | 12组输入、每组3次、每方案36请求；稳定system缓存Token占比{percent(cache["token_hit_rate"])}，动态前缀{percent(cache_base["token_hit_rate"])} | 可以写本次固定负载的观测值；去掉长期“稳定”，不要把请求命中比例当Token占比 |',
        f'| 完成率71%→89% | 默认配置关闭/开启保护分别{d0["successes"]}/{d0["success_n"]}和{d1["successes"]}/{d1["success_n"]}；授权补充分别{c0["successes"]}/{c0["success_n"]}和{c1["successes"]}/{c1["success_n"]} | 不支持历史71→89；本次指标名应是“13类开发任务的机器断言通过率” |',
        '| 定位30分钟→5分钟 | 没有真人盲测计时；只测了真实事件与本地导出的Trace关联 | 删除分钟改善；可写已实现并验证请求级追踪关联 |', '',
        '## 全部观测统计', '',
        '| 环境 | Suite | Variant | 独立case | 观测数 | 通过/总数 | Mean s | Median s | P90 s |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for name, path in runs:
        for g in data[name]['groups']:
            s = g['metrics'].get('elapsed_s', {})
            lines.append(f'| {name} | {g["suite"]} | {g["variant"]} | {g["unique_cases"]} | {g["observations"]} | {g["successes"]}/{g["success_n"]} | {number(s.get("mean"))} | {number(s.get("median"))} | {number(s.get("p90"))} |')
    lines += ['', 'faults 的总体通过率只是人为均衡的故障矩阵比例，**不是线上任务完成率**；cache 的 success 表示响应和缓存usage可用，不表示购物任务完成。', '',
        '## 缓存：冷暖效应与完整流程', '',
        '| 方案 | 第1轮观测 | 第2轮重复 | 第3轮重复 | 总缓存Token/输入Token |', '|---|---:|---:|---:|---:|']
    for variant in ('dynamic_system_prefix', 'stable_system_hint'):
        item = group('原配置', 'cache', variant)['cache']
        windows = [percent(w['token_hit_rate']) for w in item['windows']]
        lines.append(f'| {variant} | {" | ".join(windows)} | {item["cached_tokens"]}/{item["input_tokens"]} |')
    lines += ['', '第一轮不是严格冷缓存：不能清空服务端缓存，且前序Agent/pilot可能预热部分公共前缀。两方案内容相同但位置不同；热重复收敛不能被解读为稳定前缀永远优于另一组。',
        '前缀机制实验没有全套工具schema和真实多轮动态事实。完整Agent模型用量另外如下，且高缓存不代表任务完成：', '',
        '| 环境 | 方案 | 模型请求 | 有缓存字段 | 缓存Token占比 |', '|---|---|---:|---:|---:|']
    for name in ('原配置', '授权补充'):
        for u in data[name]['model_usage']:
            if u['suite'] == 'agent':
                lines.append(f'| {name} | {u["variant"]} | {u["requests"]} | {u["reported"]} | {percent(u["cache_token_rate"])} |')
    lines += ['', 'usage字段依据[DeepSeek官方文档](https://api-docs.deepseek.com/guides/kv_cache/)，保存原始字段；缺失不当成0。', '',
        '## 任务、并发与失败证据', '',
        '原配置product_search_tool为非只读工具，但allow_business_tools未包含它。源码与AgentState中的tool_call.state=asking互相印证，最终等待许可，没有真实搜索。该类失败没有从分母删除。',
        '机器断言核对工具参数/结果、SQLite订单和库存、偏好存储与最终文本；不使用旧LLM judge的PASS。它不涵盖所有自然语言质量要求，最终回答已保存供人工复核。', '',
        '既有order-full-cycle脚本第三轮只说“把这个订单取消”，没有第四轮“确认取消”。当前Agent可能按确认策略再次询问，因此cancelled_and_restored断言不通过并不自动代表取消工具故障。本轮保留预登记三轮脚本与分母，如实标记任务未在该脚本内完成；若评估完整确认流程，应另行冻结含确认回复的新数据集，不事后把该项改判成功。', '',
        '并发实验还要求每个专家的最终正文是可直接解析的JSON（允许外层代码围栏），其中hits非空且ID能回溯工具证据。带额外说明段落的JSON会不通过这一严格输出契约；这类格式失败不能解释为商品工具没有执行。耗时配对只纳入两组都满足契约的样本，不能据此推断所有任务的端到端收益。', '',
        '| 授权补充case | 保护关闭 | 保护开启 |', '|---|---:|---:|']
    task_rows = [r for r in load_scored_rows(consented) if r['suite'] == 'agent']
    for case in sorted({r['case_id'] for r in task_rows}):
        counts = []
        for variant in ('protection_off', 'protection_on'):
            subset = [r for r in task_rows if r['case_id'] == case and r['variant'] == variant]
            counts.append(f'{sum(r["success"] for r in subset)}/{len(subset)}')
        lines.append(f'| {case} | {" | ".join(counts)} |')
    lines += ['', '授权补充失败断言（每条原始输出均可检查）：', '']
    for name in ('原配置', '授权补充'):
        review = data[name].get('scoring_review')
        if review:
            lines.append(f'- {name}评分复核：对全部{review["rows_reviewed"]}条Agent观测统一按金额数值等价校验，修正{review["changed_outcomes"]}条整体判分。原字符串匹配可能误判154与154.0；原始results.jsonl不改，逐条新旧检查见score-review.jsonl，汇总使用复核分数。')
    from collections import Counter
    failed_checks = Counter(k for row in task_rows for k, v in row.get('checks', {}).items() if not v)
    for check, count in failed_checks.most_common():
        lines.append(f'- `{check}`：{count}次。')
    for p in data['授权补充']['paired']:
        if p['suite'] == 'parallel':
            lines += ['', f'并发配对统计：`{p["comparison"]}`；两边均完成的配对数={p["paired_success_elapsed_ratio_left_over_right"]["n"]}，左方案耗时/右方案耗时均值={number(p["paired_success_elapsed_ratio_left_over_right"]["mean"])}。方案按字母排序，parallel为左、serial为右，因此小于1表示并行耗时较低；原始逐组耗时仍需结合成功率阅读。']
        if p['suite'] == 'agent':
            lines += ['', f'保护开启减关闭的通过率差={percent(p["success_delta"])}；按case聚类bootstrap 95%区间={p["clustered_bootstrap_95"]}。小开发集与多机制组合消融不能证明单一重试/熔断组件的因果提升。']
            if p['clustered_bootstrap_95'] == [0.0, 0.0]:
                lines.append('这里每个case的观测差值都为0，所以重采样区间退化为0；它不能证明真实用户总体的差异必然为0。')
    lines += ['', '## 压缩、隔离与偏好', '',
        f'- 真实压缩处理组：{compression["successes"]}/{compression["success_n"]}通过事实探针；完整模型输入的框架估计Token平均缩减{percent(metric(compression, "estimated_token_reduction"))}。这包含摘要、保留历史及system关键事实侧账本，不是单看摘要长度；不是provider计费Token。',
        f'- 压缩后可用输入中事实精确保留率均值{percent(metric(compression, "exact_fact_retention"))}；模型回答中的事实保留率均值{percent(metric(compression, "answer_fact_retention"))}。短/长历史均纳入；窗口为强制触发实验用值，不是生产128K窗口自动触发率。',
        f'- 隔离/继承历史的任务通过率：{percent(isolated["success_rate"])} / {percent(inherited["success_rate"])}；真实输入Token均值：{number(metric(isolated, "input_tokens"))} / {number(metric(inherited, "input_tokens"))}。包括无干扰对照；不能只凭Token减少宣称准确率提升。',
        f'- 偏好选择：全量/选取输入块字符均值{number(metric(memory_a, "input_chars"))} / {number(metric(memory_b, "input_chars"))}；包括SQLite读回、删除后快照和买家隔离的契约分别{memory_a["successes"]}/{memory_a["success_n"]}、{memory_b["successes"]}/{memory_b["success_n"]}通过。这里只证明存储/选择契约，不把字符数叫Token，不证明真实用户长期推荐质量。', '',
        '## 当前推荐链路的质量与代价', '',
        f'- 完整67条既有标注、各重复3次：规则/规则+BGE的平均Recall@8为{number(metric(ret_a, "recall"))} / {number(metric(ret_b, "recall"))}。',
        f'- 同口径平均查询耗时为{number(metric(ret_a, "elapsed_s"))} / {number(metric(ret_b, "elapsed_s"))}秒。模型预加载单列setup.jsonl；BGE用真实CPU推理。',
        f'- 12条semantic子集Recall@8为{number(ret_a["strata"]["semantic"]["recall"]["mean"])} / {number(ret_b["strata"]["semantic"]["recall"]["mean"])}。不能沿用旧Qdrant流程的1.000/0.361。',
        '- 旧标签有2条预算未给目的地，而当前主链把未知到手价列为待核实；没有为了得到更好成绩删除或改写这些标签。整体表是现有标签适配测试，不是独立用户效果估计。',
        '- 本次没有reranker部署、GPU/vLLM压测或线上流量；不支持相应性能简历数字。', '',
        '## 故障与可观测性', '',
        '| 注入故障类别 | 无保护业务完成 | 有保护业务完成 |', '|---|---:|---:|']
    fa, fb = group('本地', 'faults', 'protection_off'), group('本地', 'faults', 'protection_on')
    for case in fa['fault_classes']:
        lines.append(f'| {case} | {percent(fa["fault_classes"][case]["success_rate"])} | {percent(fb["fault_classes"][case]["success_rate"])} |')
    lines += ['', '仅外部API边界注入故障，真实项目重试/熔断代码运行。短等待参数为毫秒级机制实验；定时调度抖动也保留。超时及时拒绝可以降低业务完成率却符合安全契约，不能把如实报错算成功购物。流中失败不得重放；详见safe_failure及原始chunks。', '']
    for name in ('原配置', '授权补充'):
        tr = data[name]['tracing']
        lines.append(f'- {name}：{tr["business_events"]}条业务事件中，{tr["events_linked_to_exported_trace"]}条能匹配实际本地导出的trace ID，覆盖率{percent(tr["linked_event_rate"])}。')
    lines += ['- 不覆盖Redis跨进程队列、远程OTLP网络导出或真人根因判断；工具返回ERROR时SDK span仍可能标成功，因此不能只看span状态。',
              '- 人工诊断样本数为0，分钟数未测量。`diagnosis`命令只在真人实际操作时记开始/结束，不会自动制造30→5。', '',
        '## 面试表述', '',
        '> 我先明确成功标准，再给当前工程建立了能重复运行的benchmark。测试保留真实模型usage、工具事件、数据库状态和失败样本，发现HTTP成功和有回复不等于任务完成：当前搜索工具会因权限接线挂起。我保留原配置结果，并单列已授权测试环境，避免把修复条件混入基线。缓存、检索、上下文成本分别测量，不把局部数字包装成线上整体效果。', '',
        f'> 在固定12组Prompt、每组3次的实验中，稳定system加独立偏好hint的缓存Token占比为{percent(cache["token_hit_rate"])}；这是本次DeepSeek负载的结果，不是长期保证。我能展示逐请求usage和重复轮次，热缓存与首次观测分开。', '',
        '原来的71→89、30→5暂不使用。后续若修复权限或调整提示词，应冻结本报告为基线，用新源码版本重复同一任务集，而不是覆盖失败记录。', '',
        '## 根据简历仍需补的证据', '',
        '| 简历主张 | 这轮已经测到 | 仍然缺少 |', '|---|---|---|',
        '| 稳定80%的Prompt Cache命中 | 固定负载逐请求usage、三轮复用、完整Agent用量 | 跨日期和真实会话分布的连续窗口；首次与热请求比例、长历史与工具schema变化的覆盖 |',
        '| 任务完成率71%→89% | 当前版本保护开/关的机器断言对照、失败原因及数据库证据 | 原历史两版本、同一冻结任务集、原始逐任务判分；更大的独立留出任务和人工回答质量复核 |',
        '| 定位30分钟→5分钟 | 实际本地Trace与业务事件关联 | 人工盲测的开始/确认根因时间、正确性审核、同难度故障与参与者控制 |',
        '| 独立任务并发与上下文隔离收益 | 同需求串/并行、继承/隔离的真实调用对照 | 更丰富任务和负载下的稳定效果；自动派发决策正确性、跨进程队列性能未覆盖 |',
        '| 压缩保留事实、偏好跨会话复用 | 合成历史事实探针、SQLite重启读回和删除快照 | 真实长会话分布，旧摘要/旧历史中的遗忘效果，排除条件的长期语义遵从 |',
        '| 跨平台搜索、比价与筛选 | 本地种子商品和当前检索链的标注结果 | 联网跨平台样本、来源与价格时效、同款识别、实际运税准确度；本轮关闭网页发现 |',
        '| Haro工具描述Token减少85% | 不属于本仓库 | 需在Haro仓库冻结百级工具集，对比全量Schema与按需发现，并同时记录选对工具的比例 |', '',
        '## 命令与原始证据', '',
        '所有命令见 [benchmark-commands.md](benchmark-commands.md)。可执行 `python -m scripts.benchmark.report --local ... --default ... --consented ... --output docs/benchmark-results.md` 重建本报告。', '']
    for name, path in runs:
        absolute = path.resolve().as_posix()
        lines.append(f'- {name}：[原始JSONL]({absolute}/results.jsonl)、[汇总JSON]({absolute}/aggregate.json)、[完整性审核]({absolute}/verification.json)、[manifest]({absolute}/manifest.json)。')
    lines += ['', 'pilot-v1/v2/v3及未完成的live-consented-v1/v2保留，未混入上述正式结果。']
    import xml.etree.ElementTree as ET
    tests_file = ROOT / 'output/benchmarks/final-tests-after-scoring.xml'
    if tests_file.exists():
        test_run = ET.parse(tests_file).getroot().find('testsuite').attrib
        passed = int(test_run['tests'])-int(test_run['failures'])-int(test_run['errors'])-int(test_run['skipped'])
        lines += [f'测试：初始438通过、4跳过；最新全量回归{passed}通过、{test_run["skipped"]}跳过、{test_run["failures"]}失败、{test_run["errors"]}错误。记录见output/benchmarks/{tests_file.name}。']
    notes_path = Path(consented) / 'execution-notes.json'
    if notes_path.exists():
        notes = json.loads(notes_path.read_text(encoding='utf-8'))
        lines += ['', f'运行限制：一次14.3秒本地回归测试与{len(notes["overlapping_trials"])}条压缩观测重叠，具体样本及时间见[execution-notes.json]({notes_path.resolve().as_posix()})。保留所有观测；这些压缩墙钟值只作描述，不作为隔离环境的延迟收益证据。']
    calls = load_rows(Path(consented) / 'model_calls.jsonl')
    compression_errors = [r for r in calls if r.get('suite') == 'compression' and r.get('error')]
    if compression_errors:
        lines += ['', f'压缩阶段有{len(compression_errors)}次底层请求记录错误。当前provider拒绝thinking模式下的强制tool_choice，框架自动改为auto继续；失败请求及兼容重发全部保存在model_calls.jsonl，耗时没有扣除。压缩本身成功不表示首个请求没有错误。']
    Path(destination).write_text('\n'.join(lines)+'\n', encoding='utf-8')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--local', required=True)
    p.add_argument('--default', required=True)
    p.add_argument('--consented', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    make_report(args.local, args.default, args.consented, args.output)
