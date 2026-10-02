"""Export measured summaries and protocols, leaving raw model responses local."""
import json
from pathlib import Path
from .common import digest, write_json


def main():
    names=['cache-projection-v1','task-resilience-v1','preference-recall-v1',
        'context-long-v1','context-long-v2','context-long-v3','diagnosis-evidence-v1',
        'parallel-latency-v1','parallel-latency-v2','parallel-latency-v3',
        'cross-language-v1','cross-language-agent-v1','cross-language-agent-v2']
    exported={}
    for name in names:
        root=Path('output/metric-targets')/name
        entry={}
        for filename in ('protocol.json','summary.json','spending.json','cost-and-cache.json','source-hashes.json'):
            p=root/filename
            if filename in ('protocol.json','summary.json') and not p.exists():
                raise ValueError(f'Experiment not complete: {p}')
            if p.exists():
                entry[filename]=dict(path=p.as_posix(),sha256=digest(p),data=json.loads(p.read_text(encoding='utf-8')))
        for filename in ('results.json','results.jsonl','model_calls.jsonl','injections.jsonl'):
            p=root/filename
            if p.exists():
                entry[filename]=dict(path=p.as_posix(),sha256=digest(p),raw_kept_local=True)
        exported[name]=entry
    write_json('docs/metric-evidence.json',dict(status='ongoing_target_engineering',
        scope='Controlled synthetic development experiments, not production traffic; failed versions retained',
        evidence=exported))
    print('Exported experiments:',len(exported))


if __name__=='__main__':
    main()
