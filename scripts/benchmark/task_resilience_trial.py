"""End-to-end shopping contracts under equal upstream fault schedules."""
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .common import Recorder, TRIAL, write_json, digest
from .cases import agent_cases
from .live import agent_suite
from .observation import ApiCapture, install_tracing
from benchmarks.runners.context_resume import SpendingGuard


async def main():
    from app.infrastructure.llm import ThrottledChatModel
    root = Path('output/metric-targets/task-resilience-v1')
    if root.exists():
        raise ValueError('Use a new experiment version, not overwritten observations')
    rec = Recorder(root)
    cases = deepcopy(agent_cases())
    for case in cases:
        if case['id'] == 'order-full-cycle':
            case['queries'][2] = '我确认取消刚才已创建的订单，请立即执行取消并核对订单状态。'
    strata = ['normal', 'transient_once', 'pre_stream_once']
    write_json(root/'protocol.json', {'cases': cases, 'strata': strata, 'target_completion_rate': .89,
        'paired_arms': ['protection_off', 'protection_on'],
        'baseline': 'same production business code, disable project retries/fallback/harness as in live.container',
        'fault_schedule': 'Exactly one failure on first upstream invocation per case/arm; same schedule for both arms',
        'fault_scope': 'synthetic external-boundary faults, followed by real DeepSeek responses and real local tools/SQLite',
        'aggregate_scope': 'Equal-weight experimental matrix, not natural user traffic or a claim of historical 71%',
        'order_contract': 'Explicit confirmation of cancellation; no relaxation of cancellation or inventory assertions',
        'api_calls_cap': 260, 'estimated_peak_cost_cap_cny': 2,
    })
    write_json(root/'source-hashes.json', {str(p):digest(p) for p in Path('app').rglob('*.py')})
    tracer = install_tracing(rec)
    original = ThrottledChatModel._invoke_upstream
    args = SimpleNamespace(limit_cases=0, repeats=1, consent_product_search=False, task_timeout=180)
    with ApiCapture(rec, 260) as capture, SpendingGuard(capture, rec, limit=2), patch('scripts.benchmark.cases.agent_cases', lambda: deepcopy(cases)):
        for stratum in strata:
            child = Recorder(root/stratum)
            seen = set()
            async def invoke(model, *a, **kw):
                trial = TRIAL.get()
                key = (trial.get('case_id'), trial.get('variant'), trial.get('repeat'))
                if stratum != 'normal' and key not in seen:
                    seen.add(key)
                    rec.append('injections.jsonl', {**trial, 'stratum': stratum, 'boundary': 'first_upstream_call'})
                    if stratum == 'transient_once':
                        raise RuntimeError('429 rate limit (controlled task benchmark)')
                    async def broken_stream():
                        raise RuntimeError('connection reset (controlled before first stream item)')
                        yield
                    return broken_stream()
                return await original(model, *a, **kw)
            with patch.object(ThrottledChatModel, '_invoke_upstream', invoke):
                await agent_suite(child, capture, tracer, args)
    rows = [{**json.loads(line), 'stratum': stratum} for stratum in strata
            for line in (root/stratum/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    summary = {stratum: {arm: {'passed':sum(r['success'] for r in rows if r['stratum']==stratum and r['variant']==arm),
                              'total':sum(r['stratum']==stratum and r['variant']==arm for r in rows)}
                         for arm in ('protection_off','protection_on')} for stratum in strata}
    write_json(root/'summary.json', summary)
    print('SUMMARY',json.dumps(summary),flush=True)


if __name__ == '__main__':
    asyncio.run(main())
