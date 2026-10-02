"""Grade participant-provided records; never supply or infer human elapsed time."""
import argparse
import json
import math
import statistics
from pathlib import Path
from .common import digest, write_json


def grade(records,key,protocol_hash):
    seen_participants=set()
    rows=[]
    groups=set()
    incomplete=[]
    for record in records:
        participant=record.get('participant','').strip()
        group=record.get('group')
        if not participant or participant in seen_participants or group not in ('A','B'):
            raise ValueError('Participant must be unique and group A/B valid')
        if record.get('protocol_hash')!=protocol_hash or record.get('attestation') is not True:
            raise ValueError('Protocol or participant attestation missing')
        seen_participants.add(participant)
        groups.add(group)
        seen_cases=set()
        for answer in record.get('answers',[]):
            case=answer.get('case_id')
            if case not in key or case in seen_cases:
                raise ValueError('Unknown or repeated case')
            seen_cases.add(case)
            index=list(key).index(case)
            expected_condition='logs' if (index+(group=='B'))%2==0 else 'assisted'
            if answer.get('condition')!=expected_condition or answer.get('request_id')!=key[case]['request_id']:
                raise ValueError('Condition or request ID does not match assignment')
            elapsed,hidden=answer.get('elapsed_ms'),answer.get('hidden_ms')
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in (elapsed,hidden)) or hidden>elapsed+1:
                raise ValueError('Invalid timing; missing time cannot be inferred')
            correct=all(answer.get(k)==key[case][k] for k in ('component','category'))
            rows.append(dict(participant=participant,group=group,case_id=case,condition=expected_condition,
                correct=correct,elapsed_s=elapsed/1000,hidden_s=hidden/1000,reason=answer.get('reason','')))
        if seen_cases!=set(key):
            incomplete.append(dict(participant=participant,missing_cases=sorted(set(key)-seen_cases),
                                   unfinished_case=record.get('unfinished_case')))
    summary={}
    for condition in ('logs','assisted'):
        subset=[r for r in rows if r['condition']==condition]
        correct=[r for r in subset if r['correct']]
        summary[condition]=dict(observations=len(subset),correct=len(correct),
            median_s_all=statistics.median(r['elapsed_s'] for r in subset) if subset else None,
            median_s_correct=statistics.median(r['elapsed_s'] for r in correct) if correct else None,
            correct_under_5min=sum(r['elapsed_s']<=300 for r in correct))
    return dict(participants=len(seen_participants),both_groups_present=groups=={'A','B'},
        all_cases_observed_per_participant=len(rows)==len(key)*len(seen_participants),
        summary=summary,rows=rows,incomplete_records=incomplete,
        scope='Exploratory controlled human evidence-location study. Not historical production MTTR; timing is participant-browser recorded.')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--study',type=Path,default=Path('output/metric-targets/human-diagnosis-v1'))
    p.add_argument('--answers',type=Path,nargs='+',required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():raise ValueError('Choose a new output file, preserve previous records')
    records=[json.loads(f.read_text(encoding='utf-8')) for f in args.answers]
    result=grade(records,json.loads((args.study/'answer-key.json').read_text(encoding='utf-8')),digest(args.study/'protocol.json'))
    result['source_records']=[dict(path=str(f),sha256=digest(f)) for f in args.answers]
    write_json(args.output,result)
    print(json.dumps(result['summary'],ensure_ascii=False))


if __name__=='__main__':
    main()
