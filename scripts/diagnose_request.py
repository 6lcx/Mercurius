"""python -m scripts.diagnose_request --events events.jsonl --request-id ID"""
import argparse
import json
from pathlib import Path
from time import perf_counter
from app.application.diagnostics import diagnose_request


def load_events(path):
    text = Path(path).read_text(encoding='utf-8')
    if Path(path).suffix == '.jsonl':
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        value = json.loads(text)
        rows = value if isinstance(value, list) else [value]
    result = []
    for row in rows:
        if 'events' in row:
            result.extend(row['events'])
        elif 'type' in row and 'shopping_session_id' in row:
            result.append(row)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--events', required=True)
    parser.add_argument('--request-id', required=True)
    args = parser.parse_args()
    start = perf_counter()
    result = diagnose_request(load_events(args.events), args.request_id)
    result['triage_elapsed_s'] = perf_counter() - start
    print(json.dumps(result, ensure_ascii=False, indent=2))
