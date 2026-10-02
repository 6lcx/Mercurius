"""Aggregate immutable observations. Missing measurements are never zero-filled."""
from __future__ import annotations
import argparse
import csv
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from .common import write_json, SEED


def percentile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def stats(values):
    xs = [float(x) for x in values if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)]
    return dict(n=len(xs), mean=statistics.mean(xs) if xs else None,
                median=statistics.median(xs) if xs else None, p90=percentile(xs, .9))


def wilson(k, n):
    if not n:
        return None
    z = 1.959963984540054
    p = k / n
    mid = (p + z*z/(2*n))/(1+z*z/n)
    half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return [mid-half, mid+half]


def load_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def load_scored_rows(output):
    """Apply a separately audited rescore without changing raw observations."""
    from .common import digest
    output = Path(output)
    rows = load_rows(output / 'results.jsonl')
    review = output / 'score-review-manifest.json'
    if not review.exists():
        return rows
    info = json.loads(review.read_text(encoding='utf-8'))
    if info['raw_results_sha256'] != digest(output / 'results.jsonl'):
        raise ValueError('Score review is stale: raw results changed')
    review_path = output / 'score-review.jsonl'
    if info['review_sha256'] != digest(review_path):
        raise ValueError('Score review hash mismatch')
    key = lambda r: (r['suite'], r['case_id'], r['variant'], r['repeat'])
    reviews = load_rows(review_path)
    indexed = {key(r): r for r in reviews}
    agent_keys = {key(r) for r in rows if r['suite'] == 'agent'}
    if len(indexed) != len(reviews) or set(indexed) != agent_keys:
        raise ValueError('Score review must cover every agent observation exactly once')
    for row in rows:
        if key(row) in indexed:
            revised = indexed[key(row)]
            row['original_success'] = row['success']
            row['success'], row['checks'] = revised['success'], revised['checks']
    return rows


def aggregate(output):
    output = Path(output)
    rows = load_scored_rows(output)
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['suite'], row['variant'])].append(row)
    summary = []
    for (suite, variant), group in sorted(grouped.items()):
        eligible = [r for r in group if isinstance(r.get('success'), bool)]
        successes = sum(r['success'] for r in eligible)
        item = dict(suite=suite, variant=variant, observations=len(group),
                    unique_cases=len({r['case_id'] for r in group}), success_n=len(eligible),
                    successes=successes, success_rate=successes/len(eligible) if eligible else None,
                    success_wilson_descriptive=wilson(successes, len(eligible)),
                    error_count=sum(bool(r.get('error')) for r in group),
                    censored_count=sum(bool(r.get('censored')) for r in group))
        keys = sorted({k for r in group for k, v in r.items() if isinstance(v, (float, int)) and not isinstance(v, bool)} - {'repeat'})
        item['metrics'] = {key: dict(stats([r.get(key) for r in group]), missing=sum(r.get(key) is None for r in group)) for key in keys}
        item['elapsed_success_only'] = stats([r.get('elapsed_s') for r in group if r.get('success')])
        for kind in sorted({r.get('kind') for r in group if r.get('kind')}):
            item.setdefault('strata', {})[kind] = {key: stats([r.get(key) for r in group if r.get('kind') == kind]) for key in ('recall', 'mrr', 'ndcg', 'elapsed_s')}
        # Token-weighted rate uses only requests whose cache field is available.
        available = [r for r in group if r.get('cached_tokens') is not None and r.get('input_tokens')]
        item['cache'] = dict(eligible_requests=len(available), total_observations=len(group),
                             input_tokens=sum(r['input_tokens'] for r in available),
                             cached_tokens=sum(r['cached_tokens'] for r in available),
                             token_hit_rate=sum(r['cached_tokens'] for r in available)/sum(r['input_tokens'] for r in available) if available else None,
                             request_hit_rate=sum(r['cached_tokens'] > 0 for r in available)/len(available) if available else None)
        item['cache']['windows'] = []
        for repeat in sorted({r['repeat'] for r in group}):
            window = [r for r in available if r['repeat'] == repeat]
            item['cache']['windows'].append(dict(repeat=repeat, reported=len(window),
                token_hit_rate=sum(r['cached_tokens'] for r in window)/sum(r['input_tokens'] for r in window) if window else None))
        if suite == 'faults':
            item['fault_classes'] = {}
            for case in sorted({r['case_id'] for r in group}):
                subset = [r for r in group if r['case_id'] == case]
                item['fault_classes'][case] = dict(n=len(subset), success_rate=sum(r['success'] for r in subset)/len(subset),
                    safe_failure_rate=sum(r.get('safe_failure', False) for r in subset)/len(subset),
                    elapsed_s=stats([r['elapsed_s'] for r in subset]), upstream_calls=stats([r.get('upstream_calls') for r in subset]))
        summary.append(item)
    paired = []
    suites = sorted({r['suite'] for r in rows})
    for suite in suites:
        variants = sorted({r['variant'] for r in rows if r['suite'] == suite})
        if len(variants) != 2:
            continue
        indexed = {(r['case_id'], r['repeat'], r['variant']): r for r in rows if r['suite'] == suite}
        differences = defaultdict(list)
        speeds, latency_deltas = [], []
        for case, repeat, variant in indexed:
            if variant != variants[0]:
                continue
            left, right = indexed[(case, repeat, variant)], indexed.get((case, repeat, variants[1]))
            if right is not None and isinstance(left.get('success'), bool) and isinstance(right.get('success'), bool):
                differences[case].append(int(right['success']) - int(left['success']))
                if left['success'] and right['success'] and left.get('elapsed_s', 0) > 0 and right.get('elapsed_s', 0) > 0:
                    speeds.append(left['elapsed_s']/right['elapsed_s'])
                    latency_deltas.append(right['elapsed_s']-left['elapsed_s'])
        xs = [statistics.mean(v) for v in differences.values()]
        rng = random.Random(SEED)
        boot = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(2000)) if xs else []
        paired.append(dict(suite=suite, comparison=f'{variants[1]} minus {variants[0]}', independent_case_clusters=len(xs),
                           success_delta=statistics.mean(xs) if xs else None,
                           paired_success_elapsed_ratio_left_over_right=stats(speeds),
                           paired_success_elapsed_delta_right_minus_left=stats(latency_deltas),
                           clustered_bootstrap_95=[percentile(boot, .025), percentile(boot, .975)] if boot else None))
    calls = load_rows(output / 'model_calls.jsonl')
    call_groups = defaultdict(list)
    for call in calls:
        call_groups[(call.get('suite'), call.get('variant'))].append(call)
    usage = []
    for (suite, variant), group in sorted(call_groups.items(), key=str):
        available = [r for r in group if r.get('cached_tokens') is not None and r.get('input_tokens')]
        inputs = [r['input_tokens'] for r in group if r.get('input_tokens') is not None]
        outputs = [r['output_tokens'] for r in group if r.get('output_tokens') is not None]
        usage.append(dict(suite=suite, variant=variant, requests=len(group), reported=len(available),
                          input_tokens=sum(inputs) if inputs else None,
                          output_tokens=sum(outputs) if outputs else None,
                          input_tokens_missing=len(group)-len(inputs), output_tokens_missing=len(group)-len(outputs),
                          totals_scope='Sum of reported usage only; missing values are not assumed free or zero.',
                          cache_token_rate=sum(r['cached_tokens'] for r in available)/sum(r['input_tokens'] for r in available) if available else None))
    span_rows = load_rows(output / 'spans.jsonl')
    spans = [r['span'] for r in span_rows]
    trace_ids = {s.get('context', {}).get('trace_id', '').removeprefix('0x') for s in spans}
    turns = load_rows(output / 'turns.jsonl')
    events = [e for row in turns for e in row.get('events', [])]
    linked = [e for e in events if len(e.get('traceparent', '').split('-')) == 4 and e['traceparent'].split('-')[1] in trace_ids]
    human = load_rows(output / 'human-diagnosis.jsonl')
    diagnoses = [r for r in human if r.get('action') == 'finish']
    human_summary = {condition: dict(completed=sum(r.get('condition') == condition for r in diagnoses),
        correct_duration_s=stats([r.get('elapsed_s') for r in diagnoses if r.get('condition') == condition and r.get('correct') == 'yes']))
        for condition in ('logs', 'trace_and_logs')}
    result = dict(groups=summary, paired=paired, model_usage=usage,
                  tracing=dict(exported_spans=len(spans), unique_trace_ids=len(trace_ids), business_events=len(events),
                               events_linked_to_exported_trace=len(linked),
                               linked_event_rate=len(linked)/len(events) if events else None,
                               scope='in-process orchestration and agent spans; no queue/OTLP-network coverage'),
                  human_diagnosis=human_summary,
                  note='Wilson intervals are descriptive only: repeats are correlated; paired intervals cluster by case. Diagnosis time requires human observations.')
    review_manifest = output / 'score-review-manifest.json'
    result['scoring_review'] = json.loads(review_manifest.read_text(encoding='utf-8')) if review_manifest.exists() else None
    write_json(output / 'aggregate.json', result)
    from .common import digest, now
    sources = {}
    for name in ('aggregate.py', 'common.py'):
        source = Path(__file__).with_name(name)
        snapshot = output / 'aggregation-source' / name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(source.read_bytes())
        sources[name] = digest(snapshot)
    write_json(output / 'aggregation-manifest.json', dict(created_at=now(),
        script_sha256=digest(Path(__file__)), source_snapshots=sources,
        raw_sha256={p.name: digest(p) for p in output.glob('*.jsonl')}))
    flat_keys = sorted({k for r in rows for k, v in r.items() if not isinstance(v, (dict, list))})
    with (output / 'results.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=flat_keys, extrasaction='ignore')
        writer.writeheader()
        writer.writerows({k: v for k, v in row.items() if not isinstance(v, (dict, list))} for row in rows)
    lines = ['# Benchmark results', '', '| Suite | Variant | Cases | Trials | Success | Mean s | Median s | P90 s |', '|---|---|---:|---:|---:|---:|---:|---:|']
    fmt = lambda x: 'NA' if x is None else f'{x:.4f}'
    for g in summary:
        s = g['metrics'].get('elapsed_s', {})
        lines.append(f"| {g['suite']} | {g['variant']} | {g['unique_cases']} | {g['observations']} | {fmt(g['success_rate'])} | {fmt(s.get('mean'))} | {fmt(s.get('median'))} | {fmt(s.get('p90'))} |")
    lines += ['', 'Failures remain in the denominator. See aggregate.json for metric-specific sample sizes, missing usage, strata and clustered comparisons.', '', 'Human diagnosis minutes: NOT MEASURED unless separately collected.']
    (output / 'aggregate.md').write_text('\n'.join(lines), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output')
    args = parser.parse_args()
    print(json.dumps(aggregate(args.output), ensure_ascii=False, indent=2))
