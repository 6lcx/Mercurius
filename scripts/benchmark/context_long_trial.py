"""Twelve real shopping turns, compression on/off, ABBA, all calls counted."""
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from .common import Recorder, TRIAL, digest, write_json
from .context_repair import TURNS
from .live import container, database_snapshot, drain
from .observation import ApiCapture
from benchmarks.runners.context_resume import SpendingGuard


QUERIES = TURNS + [
    '预算提高到300元，继续找寄中国的防水充电露营灯，请重新检索符合要求的商品。',
    '请记住我的长期偏好：不喜欢红色商品。',
    '露营灯预算改成150元，寄中国，这一轮没有颜色例外，请重新检索符合条件的商品。',
    '我选CTX-BLUE，规格CTX-BLUE-S1，先记为待确认购买，等我确认后再下单，现在不要下单。',
    '取消刚才待确认的购买，先不买了。露营灯预算改成90元，暂时也不用重新搜索。',
    '刚才我的预算是多少？刚才取消购买的商品是什么？购买还在等我确认吗？仅回答当前状态，不要执行下单。',
]


def check(index, state, returned, searches, db, result):
    budget = state.get('price_max_major')
    pending = state.get('pending_action')
    product = state.get('product_id')
    likes = db['buyer_preferences']
    checks = [
        lambda: 'CTX-RED' in returned and len(likes)==1,
        lambda: bool(searches) and returned=={'CTX-BLUE'} and budget==150,
        lambda: not likes and 'CTX-RED' in returned,
        lambda: product=='CTX-RED' and pending=='awaiting_confirmation',
        lambda: pending=='cancelled' and budget==90,
        lambda: pending=='cancelled' and budget==90 and '90' in result.final_text,
        lambda: returned=={'CTX-RED','CTX-BLUE','CTX-EXPENSIVE'} and budget==300,
        lambda: len(likes)==1 and likes[0]['kind']=='dislike' and '红色' in likes[0]['statement'],
        lambda: bool(searches) and returned=={'CTX-BLUE'} and budget==150,
        lambda: product=='CTX-BLUE' and pending=='awaiting_confirmation',
        lambda: pending=='cancelled' and budget==90,
        lambda: pending=='cancelled' and budget==90 and 'CTX-BLUE' in result.final_text and '90' in result.final_text,
    ]
    return bool(result.success and not db['orders'] and checks[index]())


async def main(version='v2'):
    from app.application.agents.orchestrator import SubmitIntentInput
    from app.domain.buyer.preference import BuyerPreference
    from app.domain.catalog.product import Product
    from app.domain.catalog.sku import Sku
    from app.domain.catalog.money import Money
    from app.infrastructure.persistence.persistent_product_repository import PersistentProductRepository
    root = Path('output/metric-targets') / f'context-long-{version}'
    root.mkdir(parents=True, exist_ok=False)
    rec = Recorder(root)
    products = [Product(pid, '防水充电露营灯 '+color, 'Demo', '户外运动', 'CN', '便携 防水 充电 露营灯',
        ships_to=['CN'], skus=[Sku(pid+'-S1','standard',Money.from_major_units(price,'CNY'),5)], evidence={'colors':[color]})
        for pid,color,price in [('CTX-RED','red',80),('CTX-BLUE','blue',80),('CTX-EXPENSIVE','red',220)]]
    schedule = [('full_history',0),('compressed',0),('compressed',1),('full_history',1)]
    write_json(root/'protocol.json', dict(queries=QUERIES, products=[asdict(p) for p in products], schedule=schedule,
        compression_after_turns=[1,3,7,9], restart_after_turns=[4,10], indexes_zero_based=True,
        scope='Real DeepSeek, production orchestrator/tools/state/SQLite; synthetic controlled catalog; no padded history',
        baseline='Same current app with full history; compression threshold not reached in 12 turns',
        optimized='Same app, explicit compression at fixed shopping boundaries; retains current production reserve policy overridden to force compression',
        metric='All provider input tokens including compression calls, over all 12 turns; separately record cache/output/reference costs',
        quality='Every turn checks DB/state/search evidence; must not create any order',
        protocol_revision='v1 wrongly required cancelled product to remain currently selected. v2 explicitly asks last cancelled product, requires its ID in answer and cancelled state; retains v1 outcomes.',
        api_call_cap=200, estimated_peak_cost_cap_cny=1.5))
    write_json(root/'source-hashes.json',{str(p):digest(p) for p in [Path(__file__), *Path('app').rglob('*.py')]})
    with ApiCapture(rec,200) as capture, SpendingGuard(capture,rec,limit=1.5), patch(
        'app.composition.PersistentProductRepository',lambda data_dir:PersistentProductRepository(data_dir,products=products)):
        for variant,repeat in schedule:
            key=f'{variant}-{repeat}'
            child=Recorder(root/key)
            rows=[]
            async with container(child,key) as c:
                sid=buyer=key
                await c.orchestrator._preference_store.append(BuyerPreference(buyer,'dislike','不喜欢红色商品'))
                q=c.bus.subscribe(sid)
                for index,query in enumerate(QUERIES):
                    token=TRIAL.set(dict(suite='context-long',variant=variant,repeat=repeat,turn=index,phase='business'))
                    try:
                        async with asyncio.timeout(150):
                            result=await c.orchestrator.handle_intent(SubmitIntentInput(sid,buyer,'zh-CN','CNY',query,request_id=f'{key}-{index}'))
                        agent=await c.orchestrator._sessions.get_or_create(sid)
                        events=drain(q)
                        searches=[e['payload'] for e in events if e['type']=='tool.result' and e['payload'].get('tool')=='product_search_tool']
                        returned={h['product_id'] for s in searches for h in s.get('hits',[])}
                        db=database_snapshot(c.settings.data_dir/'globex.db')
                        state=agent.state.middle_context.get('current_shopping',{})
                        passed=check(index,state,returned,searches,db,result)
                        row=dict(turn=index,query=query,passed=passed,text=result.final_text,state=dict(state),events=events,database=db)
                        rows.append(row)
                        child.artifact(f'turn-{index}.json',row)
                        child.artifact(f'state-{index}.json',agent.state.model_dump(mode='json'))
                        await c.orchestrator._sessions.persist(sid)
                        if variant=='compressed' and index in (1,3,7,9):
                            TRIAL.set(dict(suite='context-long',variant=variant,repeat=repeat,turn=index,phase='summary'))
                            config=agent.context_config.model_copy(update={'trigger_ratio':.00001,'reserve_ratio':.00001})
                            async with asyncio.timeout(120):
                                await agent.compress_context(context_config=config)
                            child.artifact(f'compression-{index}.json',agent.state.model_dump(mode='json'))
                            await c.orchestrator._sessions.persist(sid)
                        if index in (4,10):
                            c.orchestrator._sessions._agents.pop(sid)
                        print(json.dumps(dict(variant=variant,repeat=repeat,turn=index,passed=passed)),flush=True)
                    finally:
                        TRIAL.reset(token)
                c.bus.unsubscribe(sid,q)
            calls=[r for r in capture.calls if r.get('variant')==variant and r.get('repeat')==repeat]
            rec.append('results.jsonl',dict(variant=variant,repeat=repeat,passed=sum(r['passed'] for r in rows),total=len(rows),
                model_calls=len(calls),api_errors=sum(bool(r.get('error')) for r in calls),
                input_tokens=sum(r.get('input_tokens') or 0 for r in calls),output_tokens=sum(r.get('output_tokens') or 0 for r in calls),
                cached_tokens=sum(r.get('cached_tokens') or 0 for r in calls),summary_calls=sum(r.get('phase')=='summary' for r in calls)))
    rows=[json.loads(s) for s in (root/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    summary={v:{k:sum(r[k] for r in rows if r['variant']==v) for k in ('passed','total','model_calls','api_errors','input_tokens','output_tokens','cached_tokens','summary_calls')}
             for v in ('full_history','compressed')}
    summary['input_reduction']=1-summary['compressed']['input_tokens']/summary['full_history']['input_tokens']
    summary['quality_passed']=all(r['passed']==r['total']==12 and not r['api_errors'] for r in rows)
    write_json(root/'summary.json',summary)
    print(json.dumps(summary),flush=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--version',default='v2',choices=['v2','v3'])
    asyncio.run(main(parser.parse_args().version))
