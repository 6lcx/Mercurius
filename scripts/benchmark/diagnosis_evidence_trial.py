"""Timed automated evidence extraction; explicitly not human MTTR."""
import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from .common import Recorder, write_json, digest
from .local import faults


async def main():
    root=Path('output/metric-targets/diagnosis-evidence-v1')
    root.mkdir(parents=True,exist_ok=False)
    expected={'transient_once':'rate_limit','persistent_transient':'rate_limit',
              'pre_stream_once':'connection_failure','partial_stream':'connection_failure',
              'tool_timeout':'timeout','circuit_recovery':'upstream_unavailable'}
    write_json(root/'protocol.json',dict(expected=expected,repeats=3,
        faults='Actual project model/tool resilience with explicitly simulated external API boundaries; zero paid calls',
        clock='Fresh Python diagnostic CLI process start through parsed output; all mixed-request records loaded each time',
        baseline='Same event stream with model.attempt_failed removed and model.stream_failed cause_code removed (legacy observation ablation)',
        acceptance='First observed failure category matches injected category and belongs to requested ID',
        limitation='Automated event triage, not human diagnosis or repair time; cannot establish historical 30 minutes',
        target_elapsed_s=300))
    write_json(root/'source-hashes.json',{str(p):digest(p) for p in [Path(__file__),Path('app/application/diagnostics.py'),Path('app/infrastructure/llm.py')]})
    await faults(Recorder(root/'faults'),repeats=3)
    records=[json.loads(s) for s in (root/'faults/results.jsonl').read_text(encoding='utf-8').splitlines()]
    events=[e for r in records for e in r['events']]
    write_json(root/'current-events.json',events)
    legacy=[]
    for e in events:
        if e['type']=='model.attempt_failed':
            continue
        value={**e,'payload':dict(e['payload'])}
        if e['type']=='model.stream_failed':
            value['payload'].pop('cause_code',None)
        legacy.append(value)
    write_json(root/'legacy-events.json',legacy)
    results=[]
    for r in records:
        if r['variant']!='protection_on' or r['case_id'] not in expected:
            continue
        request=f"{r['case_id']}-{r['repeat']}-protection_on"
        for variant in ('legacy','current'):
            start=time.perf_counter()
            proc=subprocess.run([sys.executable,'-X','utf8','-m','scripts.diagnose_request',
                '--events',str(root/f'{variant}-events.json'),'--request-id',request],capture_output=True,text=True,encoding='utf-8',timeout=30)
            elapsed=time.perf_counter()-start
            data=json.loads(proc.stdout) if proc.returncode==0 else {}
            cause=data.get('first_failure') or {}
            correct=cause.get('classification')==expected[r['case_id']] and data.get('request_id')==request
            results.append(dict(case_id=r['case_id'],repeat=r['repeat'],variant=variant,
                request_id=request,elapsed_s=elapsed,correct=correct,diagnosis=data,exit_code=proc.returncode))
            write_json(root/'results.json',results)
    summary={}
    for variant in ('legacy','current'):
        group=[r for r in results if r['variant']==variant]
        summary[variant]=dict(correct=sum(r['correct'] for r in group),total=len(group),
            median_cli_s=statistics.median(r['elapsed_s'] for r in group),max_cli_s=max(r['elapsed_s'] for r in group),
            correct_under_5min=sum(r['correct'] and r['elapsed_s']<=300 for r in group))
    write_json(root/'summary.json',summary)
    print(json.dumps(summary),flush=True)


if __name__=='__main__':
    asyncio.run(main())
