"""Prepare answer-blind human triage material; never generate human results."""
import hashlib
import json
from pathlib import Path
from app.application.diagnostics import diagnose_request
from .common import digest, write_json


def main():
    root=Path('output/metric-targets/human-diagnosis-v1')
    root.mkdir(parents=True,exist_ok=False)
    source=Path('output/metric-targets/diagnosis-evidence-v1/current-events.json')
    events=json.loads(source.read_text(encoding='utf-8'))
    ids=sorted({e['request_id'] for e in events})
    aliases={old:'R-'+hashlib.sha256(old.encode()).hexdigest()[:8] for old in ids}
    events=[{**e,'request_id':aliases[e['request_id']]} for e in events]
    selected=[('transient_once-0-protection_on','rate_limit','model'),
        ('tool_timeout-1-protection_on','timeout','tool'),
        ('pre_stream_once-1-protection_on','connection_failure','model'),
        ('circuit_recovery-2-protection_on','upstream_unavailable','tool'),
        ('persistent_transient-2-protection_on','rate_limit','model'),
        ('partial_stream-0-protection_on','connection_failure','model')]
    protocol=dict(source_sha256=digest(source),cases=6,participants_required_for_counterbalance=2,
        comparison='Same source events; raw mixed-request NDJSON vs current per-request evidence tool',
        assignment='A alternates logs/assisted; B reverses conditions; one viewing per case per participant',
        clock='From participant Start click to answer Submit click, browser monotonic clock; background time retained',
        outcome='Correct earliest failing component and observed fault category, not repair completion',
        limits='Simulated external fault boundaries; exploratory human usability study, not historical production MTTR',
        evidence_rule='Only participant-supplied records count. Do not fill in times, infer them, or make QA/AI activity a human observation.',
        no_target_duration_shown=True)
    write_json(root/'protocol.json',protocol)
    key={}
    cases=[]
    for i,(old,category,component) in enumerate(selected):
        case_id=f'D{i+1:02}'
        request=aliases[old]
        key[case_id]=dict(request_id=request,category=category,component=component)
        cases.append(dict(case_id=case_id,request_id=request,assisted=diagnose_request(events,request)))
    write_json(root/'answer-key.json',key)
    # Answers are kept in a separate reviewer file, never embedded in the page.
    data=dict(protocol_hash=digest(root/'protocol.json'),cases=cases,
              logs='\n'.join(json.dumps(e,ensure_ascii=False) for e in events))
    encoded=json.dumps(data,ensure_ascii=False).replace('<','\\u003c')
    template=Path('benchmarks/templates/human_diagnosis.html').read_text(encoding='utf-8')
    (root/'start.html').write_text(template.replace('__DATA__',encoded),encoding='utf-8')
    print(root/'start.html')


if __name__=='__main__':
    main()
