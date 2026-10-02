"""Human diagnosis clock. No automatic finish or invented human durations."""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
from .common import Recorder, now


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('start', 'finish'):
        a = sub.add_parser(action)
        a.add_argument('--output', required=True)
        a.add_argument('--participant', required=True)
        a.add_argument('--case', required=True)
        if action == 'start':
            a.add_argument('--condition', choices=['logs', 'trace_and_logs'], required=True)
        else:
            a.add_argument('--root-cause', required=True)
            a.add_argument('--correct', choices=['yes', 'no', 'unreviewed'], default='unreviewed')
    args = p.parse_args()
    rec = Recorder(args.output)
    events_path = rec.output / 'human-diagnosis.jsonl'
    records = [json.loads(s) for s in events_path.read_text(encoding='utf-8').splitlines()] if events_path.exists() else []
    prior = [r for r in records if r['participant'] == args.participant and r['case_id'] == args.case]
    row = dict(action=args.action, participant=args.participant, case_id=args.case, timestamp=now())
    if args.action == 'start':
        if prior:
            p.error('This participant has already seen this case; choose a new case.')
        row['condition'] = args.condition
    else:
        if len(prior) != 1 or prior[0]['action'] != 'start':
            p.error('Exactly one unmatched start is required.')
        row.update(condition=prior[0]['condition'], root_cause=args.root_cause, correct=args.correct,
                   elapsed_s=(datetime.now(timezone.utc)-datetime.fromisoformat(prior[0]['timestamp'])).total_seconds())
    rec.append('human-diagnosis.jsonl', row)
    from .aggregate import aggregate
    aggregate(rec.output)
    print(json.dumps(row, ensure_ascii=False))


if __name__ == '__main__':
    main()
