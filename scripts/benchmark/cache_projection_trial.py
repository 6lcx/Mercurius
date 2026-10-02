"""AB/BA real-runtime experiment for lossless fact-input projection."""
import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from .common import digest, write_json
from .context_repair import run, TURNS


async def main():
    root = Path('output/metric-targets/cache-projection-v1')
    if root.exists():
        raise ValueError('This experiment already exists; preserve prior observations')
    root.mkdir(parents=True)
    original = Path('benchmarks/baselines/critical_facts_before_projection.py').read_text(encoding='utf-8')
    baseline = root/'baseline_critical_facts.py'
    baseline.write_text(original, encoding='utf-8')
    spec = importlib.util.spec_from_file_location('cache_projection_baseline', baseline)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from app.application.agents import critical_facts
    optimized = critical_facts.CriticalFactsMiddleware
    sequence = [('baseline', 1), ('optimized', 1), ('optimized', 2), ('baseline', 2)]
    write_json(root/'protocol.json', {
        'sequence': sequence, 'turns': TURNS, 'target_cached_input_token_ratio': .8,
        'quality_gate': 'all six state/product/store acceptance turns pass; two real compressions',
        'baseline_source_sha256': digest(baseline),
        'optimized_source_sha256': digest(Path('app/application/agents/critical_facts.py')),
        'change': 'Model input fact projection only; complete ledger and real tool results preserved',
        'provider_cache': 'Cannot flush provider cache; AB/BA order and all run ratios retained',
        'budget': 'Each constituent run has 40 API-call cap and 1 CNY safety ceiling; expected total far below ceiling',
    })
    results = []
    for arm, repeat in sequence:
        name = f'metric-cache-{arm}-{repeat}'
        with patch.object(critical_facts, 'CriticalFactsMiddleware', module.CriticalFactsMiddleware if arm == 'baseline' else optimized):
            await run(name)
        source = Path('output/context-repair-v1')/name
        result = json.loads((source/'summary.json').read_text(encoding='utf-8'))
        calls = [json.loads(s) for s in (source/'model_calls.jsonl').read_text(encoding='utf-8').splitlines()]
        result.update(arm=arm, repeat=repeat, path=str(source),
                      input_tokens=sum(c.get('input_tokens') or 0 for c in calls),
                      cached_tokens=sum(c.get('cached_tokens') or 0 for c in calls),
                      output_tokens=sum(c.get('output_tokens') or 0 for c in calls))
        results.append(result)
        write_json(root/'results.json', results)
        print('ARM_RESULT', json.dumps(result), flush=True)
    write_json(root/'summary.json', {arm: {
        'input_tokens': sum(r['input_tokens'] for r in results if r['arm'] == arm),
        'cached_input_token_ratio': sum(r['cached_tokens'] for r in results if r['arm'] == arm)/sum(r['input_tokens'] for r in results if r['arm'] == arm),
        'all_acceptance_passed': all(r['passed'] == r['total'] for r in results if r['arm'] == arm),
        'each_run_cache_ratio': [r['cache_input_token_ratio'] for r in results if r['arm'] == arm],
    } for arm in ('baseline', 'optimized')})


if __name__ == '__main__':
    asyncio.run(main())
