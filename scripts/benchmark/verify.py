"""Audit measurement completeness, not business success. Failures are valid data."""
import argparse
import json
from pathlib import Path
from .common import digest, write_json
from .aggregate import load_rows, load_scored_rows


def verify(output):
    root = Path(output).resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    args = manifest['args']
    suites = args['suite'].split(',')
    suites = {'local': ['retrieval', 'memory', 'faults'],
              'live': ['agent', 'cache', 'parallel', 'compression'],
              'all': ['retrieval', 'memory', 'faults', 'agent', 'cache', 'parallel', 'compression']}.get(args['suite'], suites)
    planned = dict(retrieval=67, memory=12, faults=8, agent=13, cache=12, parallel=3, compression=6, isolation=6)
    expected = {}
    for suite in suites:
        count = min(planned[suite], args.get('limit_cases') or planned[suite]) if suite in ('agent', 'cache', 'parallel', 'compression', 'isolation') else planned[suite]
        repeats = args['fault_repeats'] if suite == 'faults' else args['compression_repeats'] if suite in ('compression', 'isolation') else args['repeats']
        expected[suite] = count * repeats * 2
    rows = load_rows(root / 'results.jsonl')
    errors = []
    if manifest['status'] != 'finished':
        errors.append('runner did not finish')
    if not manifest.get('source_unchanged'):
        errors.append('business sources changed during execution')
    keys = [(r['suite'], r['case_id'], r['variant'], r['repeat']) for r in rows]
    if len(keys) != len(set(keys)):
        errors.append('duplicate trial keys')
    for suite, n in expected.items():
        count = sum(r['suite'] == suite for r in rows)
        if count != n:
            errors.append(f'{suite}: expected {n}, actual {count}')
    for row in rows:
        if not isinstance(row['success'], bool):
            errors.append('non-boolean outcome')
        if row.get('artifact'):
            artifact = (root / row['artifact']).resolve()
            if not artifact.is_relative_to(root) or not artifact.is_file():
                errors.append('missing/outside artifact: ' + row['artifact'])
    source_missing = []
    for name, sha in manifest['files'].items():
        path = root / 'source' / name
        if not path.is_file() or digest(path) != sha:
            source_missing.append(name)
    if source_missing:
        errors.append(f'{len(source_missing)} source snapshots unavailable/mismatched')
    review_path = root / 'score-review-manifest.json'
    if review_path.exists():
        try:
            load_scored_rows(root)
            review_info = json.loads(review_path.read_text(encoding='utf-8'))
            if digest(root / 'score-review-evaluator.py') != review_info['evaluator_sha256']:
                errors.append('score review evaluator snapshot mismatch')
            if review_info.get('rescorer_sha256') and digest(root / 'score-review-rescorer.py') != review_info['rescorer_sha256']:
                errors.append('score review program snapshot mismatch')
            for review in load_rows(root / 'score-review.jsonl'):
                if digest(root / review['artifact']) != review['artifact_sha256']:
                    errors.append('score review evidence changed')
        except (ValueError, OSError, KeyError) as exc:
            errors.append('invalid score review: ' + str(exc))
    calls = load_rows(root / 'model_calls.jsonl')
    ids = [r['call_id'] for r in calls]
    if len(ids) != len(set(ids)):
        errors.append('duplicate model call IDs')
    for row in calls:
        cache, inputs = row.get('cached_tokens'), row.get('input_tokens')
        if cache is not None and (inputs is None or not 0 <= cache <= inputs):
            errors.append('invalid cached token count')
    report = dict(valid=not errors, errors=errors, expected=expected, actual_rows=len(rows),
                  model_calls=len(calls), missing_source_snapshots=source_missing,
                  note='Completeness of measurement only; business failures remain failures.')
    write_json(root / 'verification.json', report)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('output')
    args = p.parse_args()
    report = verify(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['valid'] else 1)
