"""Validate cited acceptance judgments and build a report from immutable evidence."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
from .common import ROOT, digest, now, write_json
from .aggregate import load_rows, stats


def resolve_pointer(document, pointer):
    if pointer == '':
        return document
    if not pointer.startswith('/'):
        raise ValueError('Evidence locator must be a JSON pointer')
    value = document
    for part in pointer[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def validate_review(root, review):
    root = Path(root)
    observed = load_rows(root/'observations.jsonl')
    rows = {r['case_id']: r for r in observed}
    judgments = review['cases']
    if len(rows) != len(observed) or len(judgments) != len(rows) or {j['case_id'] for j in judgments} != set(rows):
        raise ValueError('Every observed case requires exactly one review')
    if review['reviewer'] != {'kind':'AI', 'name':'Codex', 'independent':False, 'human_reviewed':False}:
        raise ValueError('Do not mislabel this review as independent or human')
    for judgment in judgments:
        row = rows[judgment['case_id']]
        artifact = (root/row['artifact']).resolve()
        if not artifact.is_relative_to(root.resolve()) or digest(artifact) != row['artifact_sha256'] or digest(artifact) != judgment['artifact_sha256']:
            raise ValueError('Evidence artifact changed')
        data = json.loads(artifact.read_text(encoding='utf-8'))
        expected = len(data['case']['criteria'])
        checks = judgment['criteria']
        if len(checks) != expected or sorted(c['index'] for c in checks) != list(range(expected)):
            raise ValueError('Missing or duplicated criterion')
        if judgment['delivery'] not in ('fulfilled','partial','unfulfilled'):
            raise ValueError('Invalid delivery state')
        for check in checks + judgment.get('additional_findings', []):
            if check['status'] not in ('pass','fail','insufficient_evidence') or not check['explanation'] or not check['evidence']:
                raise ValueError('Every criterion needs a verdict, reason and cited evidence')
            for evidence in check['evidence']:
                source = data
                if evidence.get('file'):
                    path = (root/evidence['file']).resolve()
                    if not path.is_relative_to(root.resolve()) or digest(path) != evidence['file_sha256']:
                        raise ValueError('Linked evidence changed or outside run')
                    source = json.loads(path.read_text(encoding='utf-8'))
                value = resolve_pointer(source, evidence['pointer'])
                content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                if not evidence['quote'] or evidence['quote'] not in content:
                    raise ValueError(f'Unsupported quotation: {judgment["case_id"]} {check.get("index", "additional_finding")}')
        if judgment['delivery']=='fulfilled' and not all(c['status']=='pass' for c in checks):
            raise ValueError('Fulfilled requires all acceptance criteria to pass')
    return rows


def report(root):
    root = Path(root).resolve()
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    review = json.loads((root/'review.json').read_text(encoding='utf-8'))
    rows = validate_review(root, review)
    if manifest['status'] != 'finished' or len(rows) != manifest['planned_cases'] or not manifest['source_unchanged']:
        raise ValueError('Incomplete run or modified business source')
    for name, sha in manifest['files'].items():
        if digest(root/'source'/name) != sha:
            raise ValueError('Frozen source mismatch: '+name)
    calls = load_rows(root/'model_calls.jsonl')
    searches = load_rows(root/'web-searches.jsonl')
    for search in searches:
        if digest(root/search['artifact']) != search['sha256']:
            raise ValueError('Web snapshot changed')
    if len({r['call_id'] for r in calls}) != len(calls):
        raise ValueError('Duplicate model call IDs')
    checks = [c for j in review['cases'] for c in j['criteria']]
    summary = dict(cases=len(rows), origin='synthetic', reviewer=review['reviewer'],
        delivery={s:sum(j['delivery']==s for j in review['cases']) for s in ('fulfilled','partial','unfulfilled')},
        criterion_verdicts={s:sum(c['status']==s for c in checks) for s in ('pass','fail','insufficient_evidence')},
        all_criteria_pass_cases=sum(all(c['status']=='pass' for c in j['criteria']) for j in review['cases']),
        elapsed_s=stats([r['elapsed_s'] for r in rows.values()]), model_requests=len(calls),
        reported_input_tokens=sum(r['input_tokens'] for r in calls if r.get('input_tokens') is not None),
        missing_usage=sum(r.get('input_tokens') is None for r in calls), web_searches=len(searches),
        web_pages_observed=sum(s['page_count'] for s in searches),
        source_unchanged=True, reviewed_at=review['reviewed_at'])
    write_json(root/'summary.json', summary)
    verified = dict(valid=True, checked_at=now(), cases=len(rows), criteria=len(checks),
        observations_sha256=digest(root/'observations.jsonl'),review_sha256=digest(root/'review.json'),
        reviewer_script_sha256=digest(Path(__file__)), scope='Integrity and evidence-reference checks; does not establish independent human agreement.')
    write_json(root/'verification.json',verified)
    (root/'review-validator.py').write_bytes(Path(__file__).read_bytes())
    with (root/'acceptance.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['case_id','mode','delivery','criterion_index','criterion','verdict','reason','evidence_json','elapsed_s'])
        for judgment in review['cases']:
            row=rows[judgment['case_id']]
            data=json.loads((root/row['artifact']).read_text(encoding='utf-8'))
            for check in judgment['criteria']:
                writer.writerow([judgment['case_id'],row['mode'],judgment['delivery'],check['index'],data['case']['criteria'][check['index']],
                    check['status'],check['explanation'],json.dumps(check['evidence'],ensure_ascii=False),row['elapsed_s']])
    labels={'fulfilled':'已完成','partial':'部分完成','unfulfilled':'未完成'}
    lines=['# 购物需求验收 v1','',
        '24个由Codex编写的合成购物需求，真实模型调用；6题启用真实联网，18题测试本地行为。业务源码未修改。AI编写与AI复核不是独立人工盲测，以下结果不称真实用户满意度。', '',
        f'交付：已完成{summary["delivery"]["fulfilled"]}、部分完成{summary["delivery"]["partial"]}、未完成{summary["delivery"]["unfulfilled"]}。',
        f'72条验收标准：通过{summary["criterion_verdicts"]["pass"]}、未通过{summary["criterion_verdicts"]["fail"]}、证据不足{summary["criterion_verdicts"]["insufficient_evidence"]}；全部验收项通过的案例{summary["all_criteria_pass_cases"]}/24。诚实说明无法完成不等于已完成购物交付。',
        f'模型请求{len(calls)}次，缺usage字段{summary["missing_usage"]}次；真实网页search {len(searches)}次，累计观察页面{summary["web_pages_observed"]}条（可能有重复）。',
        f'任务耗时mean/median/P90：{summary["elapsed_s"]["mean"]:.2f}/{summary["elapsed_s"]["median"]:.2f}/{summary["elapsed_s"]["p90"]:.2f}秒；含失败与部分完成，不代表成功购物的生产SLA。', '',
        '## 逐题结果','', '| 用例 | 模式 | 交付 | 验收项 | 结论 |','|---|---|---|---|---|']
    for judgment in review['cases']:
        row=rows[judgment['case_id']]
        passed=sum(c['status']=='pass' for c in judgment['criteria'])
        lines.append(f'| {judgment["case_id"]} | {row["mode"]} | {labels[judgment["delivery"]]} | {passed}/3 | {judgment["summary"]} |')
    lines+=['','## 验收依据','']
    for judgment in review['cases']:
        row=rows[judgment['case_id']]
        data=json.loads((root/row['artifact']).read_text(encoding='utf-8'))
        lines += [f'### {judgment["case_id"]}', '',f'[完整对话与工具证据]({(root/row["artifact"]).as_posix()})','']
        for query in data['case']['turns']:
            lines.append('> '+query)
        lines.append('')
        for check in judgment['criteria']:
            refs='；'.join(f'`{e.get("file", row["artifact"])}{e["pointer"]}`：{e["quote"][:180]}' for e in check['evidence'])
            lines.append(f'- **{check["status"]}** — {data["case"]["criteria"][check["index"]]} {check["explanation"]} 证据：{refs}')
        for finding in judgment.get('additional_findings', []):
            refs='；'.join(f'`{e.get("file", row["artifact"])}{e["pointer"]}`：{e["quote"][:180]}' for e in finding['evidence'])
            lines.append(f'- 探索性发现（不改变预登记评分）：{finding["explanation"]} 证据：{refs}')
        lines.append('')
    lines+=['## 使用边界','',
        '- 题目与标准在运行前冻结，未根据回答改题或挑选通过项；只对本次固定合成需求负责，不支持历史71%→89%或线上泛化。',
        '- 商品查询在测试环境预授权；交易仍经过脚本中的明确确认，本地订单不是实际购买。',
        '- 网页证据是当时采集的商家声明；未知运税、保修、库存与商家真伪不能由页面存在推出。',
        '- 每条判断附原文和JSON定位，验证器检查引用与哈希，但最终语义判断仍是AI复核，需要时可由独立人员再审。','',
        f'[冻结用例]({(root/"frozen-cases.json").as_posix()}) · [原始记录]({(root/"observations.jsonl").as_posix()}) · [验收CSV]({(root/"acceptance.csv").as_posix()}) · [完整性校验]({(root/"verification.json").as_posix()})']
    destination=root/'report.md'
    destination.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('output')
    print(json.dumps(report(p.parse_args().output),ensure_ascii=False,indent=2))
