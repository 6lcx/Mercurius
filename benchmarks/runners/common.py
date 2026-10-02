from __future__ import annotations
import json
import random
import shutil
import platform
from collections import Counter
from pathlib import Path
from scripts.benchmark.common import ROOT, Recorder, digest, now, write_json
from scripts.benchmark.aggregate import stats, load_rows

SEED=20261002


def create_run(name, config):
    root=ROOT/'benchmarks/raw'/name
    if root.exists() and any(root.iterdir()):
        raise ValueError('Never overwrite a run; choose a new name')
    rec=Recorder(root)
    files=[p for folder in ('app','benchmarks/runners','benchmarks/datasets','scripts/benchmark')
           for p in (ROOT/folder).rglob('*') if p.is_file() and p.suffix in ('.py','.json') and '__pycache__' not in p.parts]
    files += [ROOT/'docs/benchmark_architecture_audit.md',ROOT/'docs/benchmark_engineering_plan.md',ROOT/'uv.lock',ROOT/'pyproject.toml']
    hashes={p.relative_to(ROOT).as_posix():digest(p) for p in files}
    manifest=dict(benchmark_version='engineering-v1',timestamp=now(),status='running',seed=SEED,
        git_commit=None,git_dirty=None,git_note='No Git metadata available in supplied project directory',
        code_hashes=hashes,dataset_hashes={k:v for k,v in hashes.items() if k.startswith('benchmarks/datasets/')},
        environment=dict(python=platform.python_version(),platform=platform.platform()),
        model=config.get('model'),model_config=config.get('model_config'),config_sha256=None,
        origin='Codex synthetic',independent_human_labels=False)
    for relative in hashes:
        target=root/'source'/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/relative,target)
    write_json(root/'config.json',config)
    manifest['config_sha256']=digest(root/'config.json')
    write_json(root/'manifest.json',manifest)
    (root/'raw_results.jsonl').touch()
    return rec,manifest


def finish(rec,manifest,error=None):
    manifest.update(status='failed' if error else 'finished',finished_at=now(),error=error,
        app_unchanged=all(digest(ROOT/k)==v for k,v in manifest['code_hashes'].items() if k.startswith('app/')))
    write_json(rec.output/'manifest.json',manifest)
    aggregate(rec.output)


def order(cases,arms,repeats=1):
    rng=random.Random(SEED)
    for repeat in range(repeats):
        shuffled=list(cases)
        rng.shuffle(shuffled)
        for case in shuffled:
            variants=list(arms)
            rng.shuffle(variants)
            for variant in variants:
                yield repeat,case,variant


def result(rec,**row):
    row.setdefault('timestamp',now())
    row.setdefault('repeat',0)
    row.setdefault('failure_category',None if row.get('success') else 'contract_failed')
    rec.append('raw_results.jsonl',row)
    print(json.dumps({k:row.get(k) for k in ('suite','case_id','variant','success','failure_category','elapsed_s')},ensure_ascii=False),flush=True)


def aggregate(root):
    root=Path(root)
    rows=load_rows(root/'raw_results.jsonl')
    groups={}
    for row in rows:
        groups.setdefault((row['suite'],row['variant']),[]).append(row)
    output=[]
    for (suite,variant),items in groups.items():
        successes=sum(bool(r['success']) for r in items)
        out=dict(suite=suite,variant=variant,N=len(items),independent_cases=len({r['case_id'] for r in items}),
            successes=successes,failures=len(items)-successes,success_rate=successes/len(items),
            latency_s=stats([r['elapsed_s'] for r in items]),
            failure_categories=dict(Counter(r['failure_category'] for r in items if not r['success'])))
        for key in ('input_tokens','output_tokens','model_calls','tool_calls','summary_calls','upstream_calls','fallback_count',
                    'retry_count','blocked_calls','loop_hints','prevented_calls','correct_fields','total_fields'):
            vals=[r.get(key) for r in items]
            out[key]=dict(sum=sum(vals),mean=sum(vals)/len(vals)) if all(v is not None for v in vals) else None
        output.append(out)
    write_json(root/'aggregate.json',dict(groups=output,N=len(rows),exploratory=True,
        note='Repeated deterministic fault schedules are not independent tasks; no production generalization.'))
    return output


def usage(calls):
    def total(key):
        return sum(c[key] for c in calls) if all(c.get(key) is not None for c in calls) else None
    return dict(model_calls=len(calls),input_tokens=total('input_tokens'),output_tokens=total('output_tokens'),
                cached_tokens=total('cached_tokens'),usage_missing=sum(c.get('input_tokens') is None for c in calls),
                actual_models=sorted({c.get('response_model') or c.get('model') for c in calls}))


def failure(error,answer=''):
    if error:
        low=str(error).lower()
        return 'measurement_budget' if 'budget_exhausted' in low else 'timeout' if 'timeout' in low else 'api_or_execution_error'
    if 'permission' in answer.lower():
        return 'permission'
    return 'contract_failed'
