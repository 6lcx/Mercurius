"""Recompute evidence, cumulative cost and paired break-even; no provider calls."""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from scripts.benchmark.common import ROOT,digest,now,write_json
from scripts.benchmark.aggregate import load_rows,stats
from .context_resume import ARMS,evaluate

NAMES={'full_history':'完整历史','summary_recent':'摘要＋近期','summary_facts':'摘要＋近期＋事实侧账本'}


def peak_cost(call,reservations):
    i,o,h=call.get('input_tokens'),call.get('output_tokens'),call.get('cached_tokens')
    if i is None or o is None:
        return reservations[call['call_id']]
    h=h if h is not None else 0
    return (h*.04+(i-h)*2+o*8)/1e6


def build(root):
    root=Path(root).resolve()
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    cases=json.loads((root/'source/benchmarks/datasets/context_resume_v1.json').read_text(encoding='utf-8'))
    rows=load_rows(root/'raw_results.jsonl')
    turns=load_rows(root/'turns.jsonl')
    calls=load_rows(root/'model_calls.jsonl')
    requests=load_rows(root/'requests.jsonl')
    reservations={r['call_id']:r['reserved_peak_cny'] for r in requests}
    assert manifest['status']=='finished' and manifest['app_unchanged']
    assert len(rows)==len(cases)*len(ARMS)==config['planned_cases']*3
    assert len(turns)==config['planned_probe_turns']
    assert len({(r['case_id'],r['variant']) for r in rows})==len(rows)
    assert len({(r['case_id'],r['variant'],r['turn']) for r in turns})==len(turns)
    assert len({r['call_id'] for r in calls})==len(calls)
    assert {r['call_id'] for r in calls}==set(reservations)
    assert digest(root/'config.json')==manifest['config_sha256']
    for rel,sha in manifest['code_hashes'].items():
        assert digest(root/'source'/rel)==sha,rel
    expected_map={(c['id'],i):t['expected'] for c in cases for i,t in enumerate(c['continuations'])}
    for row in turns:
        path=root/row['artifact']
        assert digest(path)==row['artifact_sha256']
        data=json.loads(path.read_text(encoding='utf-8'))
        assert data['expected']==expected_map[(row['case_id'],row['turn'])]
        score=evaluate(data['answer'],data['expected'])
        assert score['fields']==row['checks'] and score['schema_valid']==row['schema_valid']
    for req in requests:
        path=root/req['artifact']
        assert digest(path)==req['sha256']
        data=json.loads(path.read_text(encoding='utf-8'))
        assert data['temperature']==0 and data['extra_body']=={'thinking':{'type':'disabled'}}
    summary={}
    for arm in ARMS:
        r=[x for x in rows if x['variant']==arm]
        t=[x for x in turns if x['variant']==arm]
        c=[x for x in calls if x['variant']==arm]
        phase={}
        for name in ('summary','probe'):
            members=[x for x in c if x.get('phase')==name]
            phase[name]=dict(requests=len(members),input_tokens=sum(x.get('input_tokens') or 0 for x in members),
                output_tokens=sum(x.get('output_tokens') or 0 for x in members),
                missing_usage=sum(x.get('input_tokens') is None for x in members),
                estimated_peak_cny=sum(peak_cost(x,reservations) for x in members))
        summary[arm]=dict(cases=len(r),successful_cases=sum(x['success'] for x in r),
            probe_turns=len(t),successful_turns=sum(x['success'] for x in t),
            fields_correct=sum(x['correct_fields'] for x in t),fields_total=sum(x['total_fields'] for x in t),
            input_tokens=sum(x.get('input_tokens') or 0 for x in c),output_tokens=sum(x.get('output_tokens') or 0 for x in c),
            cached_tokens=sum(x.get('cached_tokens') or 0 for x in c),
            missing_usage=sum(x.get('input_tokens') is None for x in c),
            model_requests=len(c),errored_requests=sum(bool(x.get('error')) for x in c),
            compressions=sum(x['compressions'] for x in r),phase=phase,
            estimated_peak_cny=sum(peak_cost(x,reservations) for x in c),
            case_latency_s=stats([x['elapsed_s'] for x in r]),probe_step_latency_s=stats([x['elapsed_s'] for x in t]),
            failure_categories=dict(Counter(x['failure_category'] for x in t if not x['success'])),
            actual_models=sorted({x.get('response_model') or x.get('model') for x in c}))
    cumulative=[]
    for step in range(6):
        for arm in ARMS:
            c=[x for x in calls if x['variant']==arm and x['turn']<=step]
            cumulative.append(dict(after_probe=step+1,variant=arm,input_tokens=sum(x.get('input_tokens') or 0 for x in c),
                output_tokens=sum(x.get('output_tokens') or 0 for x in c),estimated_peak_cny=sum(peak_cost(x,reservations) for x in c),
                missing_usage=sum(x.get('input_tokens') is None for x in c)))
    paired=[]
    for case in cases:
        for arm in ARMS[1:]:
            points=[]
            for step in range(6):
                a=[x for x in calls if x['case_id']==case['id'] and x['variant']==arm and x['turn']<=step]
                b=[x for x in calls if x['case_id']==case['id'] and x['variant']=='full_history' and x['turn']<=step]
                points.append(dict(step=step+1,input_delta=sum(x.get('input_tokens') or 0 for x in a)-sum(x.get('input_tokens') or 0 for x in b),
                    peak_cny_delta=sum(peak_cost(x,reservations) for x in a)-sum(peak_cost(x,reservations) for x in b),
                    usage_complete=all(x.get('input_tokens') is not None for x in a+b)))
            paired.append(dict(case_id=case['id'],variant=arm,points=points,
                first_input_break_even=next((p['step'] for p in points if p['usage_complete'] and p['input_delta']<=0),None),
                first_cost_break_even=next((p['step'] for p in points if p['peak_cny_delta']<=0),None)))
    failures=[]
    for row in turns:
        if row['success']:
            continue
        data=json.loads((root/row['artifact']).read_text(encoding='utf-8'))
        failures.append(dict(case_id=row['case_id'],variant=row['variant'],turn=row['turn'],category=row['failure_category'],
            mismatches={k:dict(expected=data['expected'][k],actual=(data['assessment']['parsed'] or {}).get(k)) for k,v in row['checks'].items() if not v},
            artifact=row['artifact'],error=data['error']))
    write_json(root/'context_summary.json',dict(groups=summary,cumulative=cumulative,paired=paired,failures=failures))
    verification=dict(valid=True,checked_at=now(),cases=len(rows),turns=len(turns),requests=len(calls),
        source_snapshots_match=True,artifact_quotes_and_scores_recomputed=True,app_unchanged=True,
        raw_sha256=digest(root/'raw_results.jsonl'),turns_sha256=digest(root/'turns.jsonl'),model_calls_sha256=digest(root/'model_calls.jsonl'),
        report_script_sha256=digest(Path(__file__)))
    write_json(root/'verification.json',verification)
    (root/'report-generator.py').write_bytes(Path(__file__).read_bytes())
    with (root/'turns.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['case_id','variant','turn','success','correct_fields','total_fields','input_tokens','output_tokens','summary_calls','elapsed_s','failure_category','artifact'])
        writer.writeheader()
        for row in turns:
            writer.writerow({k:row.get(k) for k in writer.fieldnames})
    lines=['# 上下文压缩与事实保留：简历对应实测','',
        '本轮只验证现有上下文机制，业务代码未修改。12个合成轨迹×3组，每组72次真实后续状态探针，另计摘要请求。历史回放由Codex构造，不是21–30轮自主Agent购物对话。','',
        '## 三组结果','',
        '| 方案 | 六轮全对案例 | 全对探针 | 字段正确 | 累计输入Token（含摘要） | 累计输出Token | 模型请求 | 摘要请求 | 高峰价估算元 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        s=summary[arm]
        lines.append(f'| {NAMES[arm]} | {s["successful_cases"]}/{s["cases"]} | {s["successful_turns"]}/{s["probe_turns"]} | {s["fields_correct"]}/{s["fields_total"]} | {s["input_tokens"]:,} | {s["output_tokens"]:,} | {s["model_requests"]} | {s["phase"]["summary"]["requests"]} | {s["estimated_peak_cny"]:.4f} |')
    base=summary['full_history']
    for arm in ARMS[1:]:
        s=summary[arm]
        lines+=['',f'{NAMES[arm]}相对完整历史：本轮累计输入变化{(s["input_tokens"]/base["input_tokens"]-1)*100:+.2f}%，高峰价估算变化{(s["estimated_peak_cny"]/base["estimated_peak_cny"]-1)*100:+.2f}%。输入缩减与费用变化是不同指标，费用受缓存影响。']
    a,b=summary['summary_recent'],summary['summary_facts']
    lines+=['','## 对简历这一条的具体意义','',
        f'- **已经支持的收益**：在固定合成历史与6次后续探针中，摘要＋近期消息的累计输入为{a["input_tokens"]:,}，完整历史为{base["input_tokens"]:,}；摘要成本已计入。事实更新探针通过{a["successful_turns"]}/{a["probe_turns"]}，没有观察到本轮正确性损失。',
        '- **未支持的说法**：不能据此说压缩一定省钱或加速。完整历史在多次请求间复用缓存，摘要额外生成了输出；本轮费用与总耗时的实际方向见表。窗口空间、累计输入、计费和延迟是不同目标。',
        f'- **事实侧账本的增量**：相对仅摘要，它增加{b["input_tokens"]-a["input_tokens"]:,}输入Token，整题通过数为{b["successful_cases"]}对{a["successful_cases"]}。这批用例没有证明额外正确性收益，也不能证明机制在更长或更复杂的任务中无用。',
        '- **实现上的解释**：原有侧账本从工具调用和返回值保留ID、金额等历史证据，并动态附加system。它不是完整的当前意图状态机；用户修改与旧事实的优先级仍需要模型遵守。',
        '- **面试应讲的取舍**：历史较长时用摘要控制输入规模；是否节省费用要连同摘要输出、后续轮数和provider缓存计算。不要把“输入少了”直接解释为“费用和耗时都下降”。']
    lines+=['','## 输入、摘要与缓存成本拆分','',
        '| 方案 | 每次探针平均输入Token | 摘要输入Token | 摘要输出Token | 缓存输入/全部输入Token | 缓存Token占比 |',
        '|---|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        s=summary[arm]
        lines.append(f'| {NAMES[arm]} | {s["phase"]["probe"]["input_tokens"]/s["probe_turns"]:.1f} | {s["phase"]["summary"]["input_tokens"]:,} | {s["phase"]["summary"]["output_tokens"]:,} | {s["cached_tokens"]:,}/{s["input_tokens"]:,} | {s["cached_tokens"]/s["input_tokens"]:.2%} |')
    lines+=['','缓存Token占比不是请求命中率；此处全部输入同时包含摘要与探针，不称system-only命中率。']
    lines+=['','## 摘要开销何时摊销','',
        '以下每个点累计了12题相同数量的后续探针及此前所有摘要。达到输入Token收支平衡不等于达到费用收支平衡；缓存无法清空，价格按高峰统一估算。','',
        '| 后续探针数 | 完整历史累计输入 | 摘要累计输入 | 摘要＋事实累计输入 | 完整历史估算元 | 摘要估算元 | 摘要＋事实估算元 |',
        '|---:|---:|---:|---:|---:|---:|---:|']
    for step in range(1,7):
        group={r['variant']:r for r in cumulative if r['after_probe']==step}
        vals=[group[a] for a in ARMS]
        lines.append(f'| {step} | '+ ' | '.join(f'{x["input_tokens"]:,}' for x in vals)+' | '+' | '.join(f'{x["estimated_peak_cny"]:.4f}' for x in vals)+' |')
    lines+=['','逐题首次收支平衡（后续探针序号；“未达到”只表示本轮6次观测内未达到，并非永远不会）：','',
        '| 用例 | 方案 | 输入平衡点 | 估算费用平衡点 |','|---|---|---:|---:|']
    for item in paired:
        lines.append(f'| {item["case_id"]} | {NAMES[item["variant"]]} | {item["first_input_break_even"] or "未达到"} | {item["first_cost_break_even"] or "未达到"} |')
    lines+=['','## 耗时及错误','',
        '| 方案 | 每题六轮含摘要 mean/median/P90秒 | 错误请求 | usage缺失 |','|---|---|---:|---:|']
    for arm in ARMS:
        s=summary[arm];lat=s['case_latency_s']
        lines.append(f'| {NAMES[arm]} | {lat["mean"]:.3f}/{lat["median"]:.3f}/{lat["p90"]:.3f} | {s["errored_requests"]} | {s["missing_usage"]} |')
    lines+=['','失败全量列出，不为通过率删除；JSON字段名/标题精确匹配仍比开放式推荐严格，不能把这项状态探针当全面自然语言质量。','']
    for f in failures:
        lines.append(f'- `{f["case_id"]}` / {NAMES[f["variant"]]} / 后续第{f["turn"]+1}轮：{json.dumps(f["mismatches"],ensure_ascii=False)}；错误：{f["error"]}；[原文]({(root/f["artifact"]).as_posix()})')
    if not failures:
        lines.append('本轮没有未通过探针。该结果不证明任何长度/任何用户任务都不会丢失事实。')
    lines+=['','## 解释与边界','',
        '- full_history与summary_recent比较摘要机制；summary_recent与summary_facts比较现有事实侧账本增量。不能将所有摘要收益归于侧账本。',
        '- 同一题的6轮不是6个独立案例，每组独立设计case数12；未做显著性检验，不声称显著提升或生产泛化。',
        '- 使用受控4096窗口触发现有压缩；默认128k的触发频率未测。历史为冻结回放，探针输出不回填，以防连续测验泄漏答案；没有业务工具和真实订单。',
        '- 所有组thinking disabled、temperature=0、禁用隐式重试；保留配置模型别名及provider返回模型，不能与之前thinking配置直接拼接。',
        '- 单次执行和共享provider缓存可能影响费用与耗时；seed控制执行顺序，不保证provider完全确定。',
        '- 费用按[DeepSeek官方高峰单价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)计算，是保守估算而非账单；低峰价可能更低。无usage请求按预留额计算，不假定免费。',
        '- pilot保留原始词面误判，正式前固定refurbished等价词；正式开始后未修改题目、成功标准或业务源码。','',
        '## 复现与证据','',
        f'- [预登记协议]({(root/"source/docs/context_benchmark_protocol.md").as_posix()})',
        f'- [冻结数据]({(root/"source/benchmarks/datasets/context_resume_v1.json").as_posix()})',
        f'- [逐题原始结果]({(root/"raw_results.jsonl").as_posix()})',
        f'- [逐轮CSV]({(root/"turns.csv").as_posix()})',
        f'- [真实模型usage]({(root/"model_calls.jsonl").as_posix()})',
        f'- [全部请求与输入]({(root/"requests.jsonl").as_posix()})',
        f'- [校验记录]({(root/"verification.json").as_posix()})','',
        '重跑：`python -m benchmarks.runners.context_resume --name 新目录名 --prior-cost 0`；费用停止线3元。仅重建报告：`python -m benchmarks.runners.context_report --run benchmarks/raw/context-resume-v1`。']
    tests=root/'validation-tests.xml'
    if tests.exists():
        import xml.etree.ElementTree as ET
        suites=ET.parse(tests).getroot().findall('testsuite')
        counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
        passed=counts['tests']-counts['failures']-counts['errors']-counts['skipped']
        lines+=['',f'相关回归：{passed}通过，{counts["skipped"]}跳过，{counts["failures"]}失败，{counts["errors"]}错误。[JUnit记录]({tests.as_posix()})。该回归在付费测量全部完成后执行，没有与测量重叠。']
    report=ROOT/'docs/context_benchmark_results.md'
    report.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (root/'report.md').write_bytes(report.read_bytes())
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--run',required=True)
    print(json.dumps(build(p.parse_args().run),ensure_ascii=False,indent=2))
