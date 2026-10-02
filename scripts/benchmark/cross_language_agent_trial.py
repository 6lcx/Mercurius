"""Measure existing search-agent query rewriting on frozen English intents."""
import asyncio
import json
import re
from pathlib import Path
from .common import Recorder, TRIAL, write_json, digest, safe_error
from .live import container, drain
from .observation import ApiCapture
from benchmarks.runners.context_resume import SpendingGuard


async def main(version='v1'):
    from app.application.tools.task_dispatch_tool import build_task_dispatch_tool
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    from scripts.eval.metrics import recall_at_k
    root=Path('output/metric-targets')/f'cross-language-agent-{version}'
    root.mkdir(parents=True,exist_ok=False)
    rec=Recorder(root)
    frozen=Path('output/metric-targets/cross-language-v1/protocol.json')
    cases=json.loads(frozen.read_text(encoding='utf-8'))['cases']
    write_json(root/'protocol.json',dict(cases=cases,frozen_cases_sha256=digest(frozen),
        scope='Existing production search agent (query interpretation + retrieval + final selection), NOT isolated embedding or BM25',
        baseline='Same frozen English intents sent directly to existing raw-query retrieval, cross-language-v1',
        top_k=8, api_call_cap=48, estimated_peak_cost_cap_cny=.5))
    write_json(root/'source-hashes.json',{str(p):digest(p) for p in [Path(__file__),*Path('app').rglob('*.py'),*Path('app/application/prompts').glob('*.yml')]})
    with ApiCapture(rec,48) as capture,SpendingGuard(capture,rec,limit=.5):
        async with container(rec,'cross-language') as c:
            factory=c.orchestrator._sessions._main_factory
            dispatch=build_task_dispatch_tool(factory._search_factory,factory._trade_factory,c.bus,
                preference_store=factory._preference_store,preference_selector=factory._preference_selector)
            for case in cases:
                trial=TRIAL.set(dict(suite='cross-language-agent',case_id=case['id']))
                ctx=ShoppingContext.set(ShoppingContextSnapshot(case['id'],'cross-language','en-US','CNY',
                    request_id=case['id'],raw_query=case['english']))
                q=c.bus.subscribe(case['id'])
                error=None
                answer=''
                try:
                    async with asyncio.timeout(120):
                        reply=await dispatch('search_agent',case['english'])
                    answer='\n'.join(b.text for b in reply.content if hasattr(b,'text'))
                    payload=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',answer.strip()))
                    ids=[h['product_id'] for h in payload.get('hits',[])][:8]
                except Exception as exc:
                    error=safe_error(exc)
                    ids=[]
                finally:
                    ShoppingContext.reset(ctx)
                    TRIAL.reset(trial)
                events=drain(q)
                c.bus.unsubscribe(case['id'],q)
                evidence={h['product_id'] for e in events if e['type']=='tool.result' for h in e['payload'].get('hits',[]) if isinstance(h,dict) and 'product_id' in h}
                grounded=all(pid in evidence for pid in ids)
                artifact=rec.artifact(case['id']+'.json',dict(case=case,answer=answer,events=events,error=error))
                rec.append('results.jsonl',dict(case_id=case['id'],retrieved=ids,relevant=case['relevant'],
                    recall_at_8=recall_at_k(ids,case['relevant'],8) if grounded else 0,grounded=grounded,
                    error=error,artifact=artifact))
                print(case['id'],ids,error,flush=True)
    rows=[json.loads(s) for s in (root/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    write_json(root/'summary.json',dict(cases=len(rows),recall_at_8=sum(r['recall_at_8'] for r in rows)/len(rows),
        errors=sum(bool(r['error']) for r in rows),ungrounded=sum(not r['grounded'] for r in rows)))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--version',default='v1',choices=['v1','v2'])
    asyncio.run(main(parser.parse_args().version))
