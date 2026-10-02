"""Pre-registered context isolation contrast using actual SearchAgentFactory."""
import asyncio
import json
import time
from .common import TRIAL, pairs, write_json, safe_error
from .live import container, drain


async def isolation(rec, capture, args):
    from agentscope.message import UserMsg, Msg, TextBlock
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    cases = [dict(id=f'{kind}-{product}', kind=kind, target=product, query=query)
             for kind in ('none', 'unrelated', 'outdated')
             for product, query in (('P1008', '露营灯'), ('P1005', '充电器'))]
    if args.limit_cases:
        cases = cases[:args.limit_cases]
    write_json(rec.output / 'isolation-cases.json', cases)
    for repeat, case, variant in pairs(cases, args.compression_repeats, ('inherited_history', 'isolated_context')):
        key = f'isolation-{case["id"]}-{repeat}-{variant}'
        token = TRIAL.set(dict(suite='isolation', case_id=case['id'], variant=variant, repeat=repeat))
        # Consent is explicit in this suite: measure context effects after tool admission.
        async with container(rec, key, consent=True) as c:
            factory = c.orchestrator._sessions._main_factory._search_factory
            worker = factory.build()
            history = []
            if case['kind'] == 'unrelated':
                for i in range(8):
                    history += [UserMsg('buyer', f'此前第{i+1}次询问背包和毛巾的保养，与本次选购无关。'),
                                Msg(name='assistant', role='assistant', content=[TextBlock(text='请根据商品标签保养，保持清洁干燥。'*20)])]
            elif case['kind'] == 'outdated':
                history = [UserMsg('buyer', '旧需求：预算20元，寄到美国，不要露营灯也不要充电器。'),
                           Msg(name='assistant', role='assistant', content=[TextBlock(text='已记下这次临时要求。')])]
            if variant == 'inherited_history':
                worker.state.context.extend(history)
            demand = f'之前的临时要求全部作废。现在检索{case["query"]}，预算300元，寄到中国。请调用商品工具，返回商品标识和价格，不要下单。'
            q = c.bus.subscribe(key)
            ctx = ShoppingContext.set(ShoppingContextSnapshot(key, key, 'zh-CN', 'CNY', key))
            started, error, answer, censored = time.perf_counter(), None, '', False
            before = len(capture.calls)
            try:
                async with asyncio.timeout(args.task_timeout):
                    answer = (await worker.reply([UserMsg('buyer', demand)])).get_text_content() or ''
            except Exception as exc:
                error, censored = safe_error(exc), isinstance(exc, TimeoutError)
            finally:
                ShoppingContext.reset(ctx)
            events = drain(q)
            c.bus.unsubscribe(key, q)
            results = [e['payload'] for e in events if e['type'] == 'tool.result' and e['payload'].get('tool') == 'product_search_tool']
            hits = [h for row in results for h in row.get('hits', [])]
            target_hit = any(h['product_id'] == case['target'] for h in hits)
            within = bool(hits) and all(h.get('landed_price', {}).get('landed_total_major', float('inf')) <= 300 for h in hits)
            success = error is None and target_hit and within and case['target'] in answer
            calls = capture.calls[before:]
            artifact = rec.artifact(key+'.json', dict(history=[m.model_dump(mode='json') for m in history], demand=demand, answer=answer, events=events))
            rec.result('isolation', case['id'], variant, repeat, time.perf_counter()-started, success,
                       kind=case['kind'], input_tokens=sum(r.get('input_tokens') or 0 for r in calls),
                       output_tokens=sum(r.get('output_tokens') or 0 for r in calls), model_calls=len(calls),
                       target_hit=target_hit, budget_ok=within, artifact=artifact, error=error, censored=censored,
                       consent_product_search=True, mock=False)
        TRIAL.reset(token)
