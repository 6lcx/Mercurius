from __future__ import annotations

import contextvars
import hashlib
import importlib.metadata
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRIAL = contextvars.ContextVar('benchmark_trial', default={})
SEED = 20261002


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


class Recorder:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def append(self, name, row):
        with (self.output / name).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str, allow_nan=False) + '\n')
            stream.flush()

    def result(self, suite, case, variant, repeat, elapsed, success, **data):
        self.count += 1
        row = dict(suite=suite, case_id=case, variant=variant, repeat=repeat,
                   elapsed_s=elapsed, success=success, timestamp=now(), **data)
        self.append('results.jsonl', row)
        print(json.dumps({k: row[k] for k in ('suite', 'case_id', 'variant', 'repeat', 'success')}, ensure_ascii=False), flush=True)
        return row

    def artifact(self, name, data):
        path = self.output / 'artifacts' / name
        write_json(path, data)
        return str(path.relative_to(self.output))


def manifest(args):
    files = [p for folder in ('app', 'scripts/benchmark', 'eval', 'knowledge')
             for p in (ROOT / folder).rglob('*')
             if p.is_file() and '__pycache__' not in str(p) and p.suffix in ('.py', '.jsonl', '.yaml', '.yml', '.md')]
    files += [ROOT / 'pyproject.toml', ROOT / 'uv.lock', ROOT / 'docs/benchmark-methodology.md']
    packages = {}
    for package in ('agentscope', 'openai', 'httpx', 'fastembed', 'onnxruntime', 'qdrant-client', 'sqlalchemy', 'opentelemetry-sdk'):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    return dict(started_at=now(), status='running', seed=SEED, args=vars(args),
                python=platform.python_version(), os=platform.platform(), cpu=platform.processor(),
                logical_cpu_count=os.cpu_count(), packages=packages,
                files={str(p.relative_to(ROOT)).replace('\\', '/'): digest(p) for p in sorted(files)},
                scope='local synthetic catalog; real project/SQLite/BGE; live suites use configured provider',
                independent_cases_not_repeats=True)


def pairs(cases, repeats, variants, seed=SEED):
    import random
    rng = random.Random(seed)
    for repeat in range(repeats):
        order = list(enumerate(cases))
        rng.shuffle(order)
        for index, case in order:
            arms = list(variants)
            if (index + repeat) % 2:
                arms.reverse()
            for variant in arms:
                yield repeat, case, variant


def safe_error(error):
    # No credential values or request headers enter result files.
    import re
    value = f'{type(error).__name__}: {error}'
    for key, secret in os.environ.items():
        if any(word in key.upper() for word in ('API_KEY', 'PASSWORD', 'SECRET', 'TOKEN')) and len(secret) > 5:
            value = value.replace(secret, '[REDACTED]')
    return re.sub(r'(?:sk-|tvly-)[A-Za-z0-9_-]+', '[REDACTED]', value)[:1000]
