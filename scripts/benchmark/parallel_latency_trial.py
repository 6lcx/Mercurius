"""Paired real search workers, no concurrent test workload during timing."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import live
from .common import Recorder, write_json, digest
from .observation import ApiCapture
from benchmarks.runners.context_resume import SpendingGuard


async def main(version='v1', min_interval=None):
    root = Path('output/metric-targets') / f'parallel-latency-{version}'
    root.mkdir(parents=True, exist_ok=False)
    rec = Recorder(root)
    cases = [dict(id='travel-two', tasks=['检索旅行三件套，返回真实工具结果。', '检索降噪耳机，返回真实工具结果。']),
             dict(id='camping-four', tasks=[f'检索{q}，返回真实工具结果。' for q in ['露营灯','帐篷','登山杖','速干毛巾']]),
             dict(id='travel-four', tasks=[f'检索{q}，返回真实工具结果。' for q in ['旅行三件套','降噪耳机','充电器','背包']])]
    args = SimpleNamespace(parallel_cases=cases, limit_cases=0, repeats=2,
        task_timeout=150, consent_product_search=False)
    settings = replace(live.base_settings(), llm_max_concurrency=4)
    if min_interval is not None:
        settings = replace(settings, llm_min_interval_seconds=min_interval)
    write_json(root/'protocol.json', dict(cases=cases, repeats=2, max_concurrency=4,
        min_interval_s=settings.llm_min_interval_seconds,
        baseline='Same independent workers, same configuration, awaited sequentially',
        optimized='Existing production task_dispatch awaited concurrently',
        scope='Worker fan-out phase only; excludes main-agent planning and final synthesis, no claim of full user RT',
        schedule='Seeded case order; reverse arm order across repeats',
        acceptance='Per-worker request_id isolation: each returned item must reference its own successful search; empty output valid only when its successful searches are empty. No calls, wrong IDs, malformed output fail.',
        protocol_revision='v1 required nonempty hits and misclassified truthful no-match output; v2 fixes validation before new calls. Original v1 results retained.',
        warmup='One local embedding before timed workers, container startup excluded in both arms',
        estimated_peak_cost_cap_cny=1, api_call_cap=160))
    write_json(root/'source-hashes.json', {str(p):digest(p) for p in [*Path('app').rglob('*.py'), Path(__file__), Path(live.__file__), Path('scripts/benchmark/worker_contract.py')]})
    from app.infrastructure.embedding.local_embedding import LocalEmbeddingClient
    await LocalEmbeddingClient().embed('本地模型预热')
    with patch.object(live, 'base_settings', lambda: settings), ApiCapture(rec, 160) as capture, SpendingGuard(capture, rec, limit=1):
        await live.parallel_suite(rec, capture, args)
    rows = [json.loads(line) for line in (root/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    groups = {}
    for n in (2,4):
        ids = {c['id'] for c in cases if len(c['tasks'])==n}
        group = [r for r in rows if r['case_id'] in ids]
        times = {arm:sum(r['elapsed_s'] for r in group if r['variant']==arm) for arm in ('serial','parallel')}
        groups[str(n)] = dict(total=len(group), passed=sum(r['success'] for r in group),
            elapsed_totals_s=times, reduction=1-times['parallel']/times['serial'],
            valid_quality_comparison=all(r['success'] for r in group))
    write_json(root/'summary.json', groups)
    print(json.dumps(groups), flush=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', default='v1', choices=['v1', 'v2', 'v3'])
    parser.add_argument('--min-interval', type=float)
    args = parser.parse_args()
    asyncio.run(main(args.version, args.min_interval))
