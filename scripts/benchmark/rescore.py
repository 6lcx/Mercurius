"""Correct numeric formatting false negatives using every stored Agent trace.

No model calls, filtering, new target values, or edits to raw results.
"""
import argparse
import json
from pathlib import Path
from .cases import evaluate
from .aggregate import load_rows
from .common import digest, write_json, now


def rescore(output):
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['status'] != 'finished':
        raise ValueError('Wait for the run to finish before reviewing scores')
    rows = load_rows(output / 'results.jsonl')
    reviews = []
    for row in rows:
        if row['suite'] != 'agent':
            continue
        artifact = output / row['artifact']
        saved = json.loads(artifact.read_text(encoding='utf-8'))
        checks = evaluate(saved['case'], saved['turns'])
        checks['no_execution_error'] = row.get('error') is None
        changed = {k: [row['checks'].get(k), value] for k, value in checks.items() if row['checks'].get(k) != value}
        if any(not k.endswith('_quoted_total') for k in changed):
            raise ValueError('Review scope exceeded: only amount formatting may change')
        reviews.append(dict(suite=row['suite'], case_id=row['case_id'], variant=row['variant'], repeat=row['repeat'],
                            original_success=row['success'], success=all(checks.values()), checks=checks,
                            changed_checks=changed, artifact=row['artifact'], artifact_sha256=digest(artifact)))
    path = output / 'score-review.jsonl'
    path.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in reviews), encoding='utf-8')
    info = dict(created_at=now(), reason='Numeric equivalence, e.g. 154 == 154.0; frozen semantic criterion unchanged.',
                raw_results_sha256=digest(output / 'results.jsonl'), review_sha256=digest(path),
                evaluator_sha256=digest(Path(__file__).with_name('cases.py')),
                rescorer_sha256=digest(Path(__file__)),
                rows_reviewed=len(reviews), changed_outcomes=sum(r['success'] != r['original_success'] for r in reviews))
    write_json(output / 'score-review-manifest.json', info)
    (output / 'score-review-evaluator.py').write_bytes(Path(__file__).with_name('cases.py').read_bytes())
    (output / 'score-review-rescorer.py').write_bytes(Path(__file__).read_bytes())
    return info


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('output')
    print(json.dumps(rescore(p.parse_args().output), ensure_ascii=False, indent=2))
