from __future__ import annotations
import argparse
import asyncio
import logging
import sys
from pathlib import Path
from .common import Recorder, manifest, write_json, now, safe_error, digest, ROOT
from .aggregate import aggregate


async def run(args, rec):
    from .local import retrieval, memory, faults
    selected = args.suite.split(',')
    if args.suite == 'local':
        selected = ['retrieval', 'memory', 'faults']
    if args.suite == 'live':
        selected = ['agent', 'cache', 'parallel', 'compression']
    if args.suite == 'all':
        selected = ['retrieval', 'memory', 'faults', 'agent', 'cache', 'parallel', 'compression']
    allowed = {'retrieval', 'memory', 'faults', 'agent', 'cache', 'parallel', 'compression', 'isolation'}
    if set(selected)-allowed:
        raise ValueError(f'Unknown suites: {set(selected)-allowed}')
    for suite in selected:
        print(f'BEGIN {suite}', flush=True)
        if suite == 'retrieval':
            await retrieval(rec, args.repeats)
        elif suite == 'memory':
            await memory(rec, args.repeats)
        elif suite == 'faults':
            await faults(rec, args.fault_repeats)
        elif suite == 'isolation':
            from .isolation import isolation
            from .observation import ApiCapture, install_tracing
            install_tracing(rec)
            with ApiCapture(rec, args.max_calls) as capture:
                await isolation(rec, capture, args)
        else:
            from .live import run_live
            await run_live(rec, suite, args)
        aggregate(rec.output)


def main():
    parser = argparse.ArgumentParser(description='Run frozen experiments, never adjust business code or expected scores.')
    parser.add_argument('--suite', default='local')
    parser.add_argument('--output', required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--fault-repeats', type=int, default=10)
    parser.add_argument('--compression-repeats', type=int, default=2)
    parser.add_argument('--limit-cases', type=int, default=0, help='pilot only; manifest marks reduced coverage')
    parser.add_argument('--max-calls', type=int, default=800)
    parser.add_argument('--task-timeout', type=float, default=180)
    parser.add_argument('--consent-product-search', action='store_true', help='Supplementary environment only: preauthorize synthetic local product tool; never mix with default runs')
    args = parser.parse_args()
    if min(args.repeats, args.fault_repeats, args.compression_repeats, args.max_calls) < 1:
        parser.error('repeats and max-calls must be positive')
    path = Path(args.output)
    if path.exists() and any(path.iterdir()):
        parser.error('Output must be new/empty. Never overwrite or silently mix prior observations.')
    rec = Recorder(path)
    info = manifest(args)
    info['pilot'] = bool(args.limit_cases)
    write_json(path / 'manifest.json', info)
    # Preserve the exact measured implementation even if the working tree later changes.
    import shutil
    for relative in info['files']:
        target = path / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    logging.basicConfig(filename=path / 'runtime.log', level=logging.WARNING, encoding='utf-8')
    try:
        asyncio.run(run(args, rec))
        info['status'] = 'finished'
    except BaseException as exc:
        info['status'] = 'runner_failed'
        info['error'] = safe_error(exc)
        raise
    finally:
        info['finished_at'] = now()
        info['observations'] = rec.count
        info['source_unchanged'] = all(digest(ROOT / p) == h for p, h in info['files'].items() if p.startswith('app/'))
        write_json(path / 'manifest.json', info)
        aggregate(path)


if __name__ == '__main__':
    main()
