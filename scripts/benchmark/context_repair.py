"""Small live acceptance of context fixes, isolated SQLite and synthetic products.

Run: python -m scripts.benchmark.context_repair --name live-v1
This is functional acceptance, not a controlled improvement benchmark.
"""
import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from .common import Recorder, TRIAL, write_json, digest
from .live import container, database_snapshot, drain
from .observation import ApiCapture
from benchmarks.runners.context_resume import SpendingGuard

TURNS = [
    '这一次买礼物可以接受红色，但不要删除我的长期偏好。帮我找防水充电露营灯，寄中国，300元以内。列出符合要求的商品。',
    '继续刚才的露营灯需求，这一轮没有颜色例外，预算改成150元，仍寄中国。请重新检索符合条件的商品。',
    '以后不用避开红色了，请删除这条长期偏好，再按150元预算继续找寄中国的防水充电露营灯。',
    '我选CTX-RED，规格CTX-RED-S1，先记为待确认购买，等我确认后再下单，现在不要下单。',
    '取消刚才待确认的购买，先不买了。露营灯预算改成90元，暂时也不用重新搜索。',
    '刚才我的预算是多少？选中的商品是什么？购买还在等我确认吗？仅回答当前状态，不要执行下单。',
]


async def run(name):
    from app.application.agents.orchestrator import SubmitIntentInput
    from app.application.memory.preference_selector import PreferenceSelector
    from app.domain.buyer.preference import BuyerPreference
    from app.domain.catalog.product import Product
    from app.domain.catalog.sku import Sku
    from app.domain.catalog.money import Money
    from app.infrastructure.persistence.persistent_product_repository import PersistentProductRepository
    from app.infrastructure.settings import load_settings
    from urllib.parse import urlsplit
    settings = load_settings()
    if urlsplit(settings.llm_base_url).hostname != 'api.deepseek.com':
        raise RuntimeError('This acceptance is authorized and budgeted for DeepSeek only')
    root = Path('output/context-repair-v1') / name
    if root.exists():
        raise RuntimeError('Choose a fresh run name; old evidence is immutable')
    rec = Recorder(root)
    products = [Product(pid, '防水充电露营灯 '+color, 'Demo', '户外运动', 'CN', '便携 防水 充电 露营灯',
                        ships_to=['CN'], skus=[Sku(pid+'-S1', 'standard', Money.from_major_units(price, 'CNY'), 5)],
                        evidence={'colors': [color]})
                for pid, color, price in [('CTX-RED', 'red', 80), ('CTX-BLUE', 'blue', 80), ('CTX-EXPENSIVE', 'red', 220)]]
    write_json(root/'protocol.json', {'turns': TURNS, 'products': [asdict(p) for p in products],
        'scope': 'real model and tools, synthetic catalog, isolated SQLite, web disabled',
        'forced_compression_after_turns': [1, 3], 'restart_after_turn': 4,
        'checks': ['turn0 red returned and dislike retained', 'turn1 only blue and budget150',
                   'turn2 dislike deleted and red returned', 'turn3 selected product and pending confirmation',
                   'turn4 cancelled and budget90', 'turn5 persisted cancelled and no orders'],
        'max_api_calls': 40, 'estimated_peak_spend_cap_cny': 1.0})
    write_json(root/'source-hashes.json', {str(p): digest(p) for p in Path('app').rglob('*')
                                        if p.suffix in ('.py', '.yml')})
    rows = []
    with ApiCapture(rec, 40) as capture, SpendingGuard(capture, rec, limit=1.0), patch(
            'app.composition.PersistentProductRepository', lambda data_dir: PersistentProductRepository(data_dir, products=products)):
        async with container(rec, 'context-repair', consent=True, web=False) as c:
            sid = buyer = 'context-repair'
            store = c.orchestrator._preference_store
            await store.append(BuyerPreference(buyer, 'dislike', '不喜欢红色商品'))
            q = c.bus.subscribe(sid)
            for index, query in enumerate(TURNS):
                print('BEGIN turn', index, flush=True)
                TRIAL.set({'suite': 'context-repair', 'turn': index})
                async with asyncio.timeout(150):
                    result = await c.orchestrator.handle_intent(SubmitIntentInput(sid, buyer, 'zh-CN', 'CNY', query,
                                                                                request_id=f'context-repair-{index}'))
                agent = await c.orchestrator._sessions.get_or_create(sid)
                events = drain(q)
                searches = [e['payload'] for e in events if e['type'] == 'tool.result' and e['payload'].get('tool') == 'product_search_tool']
                returned = {h['product_id'] for e in searches for h in e.get('hits', [])}
                db = database_snapshot(c.settings.data_dir/'globex.db')
                state = agent.state.middle_context.get('current_shopping', {})
                checks = [
                    lambda: 'CTX-RED' in returned and len(db['buyer_preferences']) == 1,
                    lambda: bool(searches) and returned == {'CTX-BLUE'} and state.get('price_max_major') == 150,
                    lambda: not db['buyer_preferences'] and 'CTX-RED' in returned,
                    lambda: state.get('product_id') == 'CTX-RED' and state.get('pending_action') == 'awaiting_confirmation',
                    lambda: state.get('pending_action') == 'cancelled' and state.get('price_max_major') == 90,
                    lambda: state.get('pending_action') == 'cancelled' and state.get('price_max_major') == 90 and '90' in result.final_text,
                ]
                passed = bool(result.success and not db['orders'] and checks[index]())
                row = dict(turn=index, query=query, text=result.final_text, passed=passed,
                           transport_success=result.success, state=dict(state), events=events, database=db)
                rows.append(row)
                rec.artifact(f'turn-{index}.json', row)
                await c.orchestrator._sessions.persist(sid)
                if index in (1, 3):
                    config = agent.context_config.model_copy(update={'trigger_ratio': .00001, 'reserve_ratio': .00001})
                    async with asyncio.timeout(120):
                        await agent.compress_context(context_config=config)
                    rec.artifact(f'compression-{index}.json', {'summary': agent.state.summary,
                        'remaining_messages': len(agent.state.context), 'middle_context': agent.state.middle_context})
                    await c.orchestrator._sessions.persist(sid)
                if index == 4:
                    c.orchestrator._sessions._agents.pop(sid)
                print('END turn', index, 'passed=', passed, flush=True)
            # Real local embedding selection, with the relevant preference older
            # than all distractors; not a paid model call or a general Recall claim.
            preferences = [BuyerPreference(buyer, 'like', text, f'2026-01-{i+1:02}') for i, text in enumerate([
                '露营灯喜欢USB-C充电和长续航', '咖啡喜欢浅烘焙', '衣服喜欢宽松版型',
                '耳机喜欢入耳式', '旅行箱喜欢硬壳', '床品喜欢纯棉', '鼠标喜欢无线'])]
            selected = await PreferenceSelector(c.embedder, relevance_enabled=True).select(preferences, '露营灯续航和充电', 1)
            write_json(root/'real-embedding-selection.json', {'query': '露营灯续航和充电',
                'preferences': [asdict(p) for p in preferences], 'selected': [p.statement for p in selected],
                'passed': bool(selected and selected[0] == preferences[0])})
    write_json(root/'summary.json', {'passed': sum(r['passed'] for r in rows), 'total': len(rows),
        'calls': len(capture.calls), 'api_errors': sum(bool(c['error']) for c in capture.calls),
        'cache_input_token_ratio': sum(c.get('cached_tokens') or 0 for c in capture.calls) /
                                 max(1, sum(c.get('input_tokens') or 0 for c in capture.calls)),
        'note': 'Functional acceptance only; not a causal comparison or stable cache guarantee.'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(run(args.name))
