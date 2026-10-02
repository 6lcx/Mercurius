"""Independent, frozen multi-turn requirements using real DeepSeek and tools."""
import argparse
import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from .common import Recorder, TRIAL, write_json, digest
from .live import container, drain, database_snapshot
from .observation import ApiCapture
from benchmarks.runners.context_resume import SpendingGuard


CASES = [
    {'id': 'budget-correction', 'turns': [
        {'query': '帮我找寄中国的防水充电露营灯，预算300元。请记录预算并搜索。', 'state': {'price_max_major': 300}, 'search': True},
        {'query': '预算改成90元，仍寄中国，请重新搜索露营灯。', 'state': {'price_max_major': 90}, 'search_empty': True, 'oracle_budget': 90, 'forbidden': ['CTX-EXPENSIVE']},
    ]},
    {'id': 'selection-switch', 'turns': [
        {'query': '我选CTX-RED，规格CTX-RED-S1，先记为待确认购买，现在不要下单。', 'state': {'product_id': 'CTX-RED', 'sku_id': 'CTX-RED-S1', 'pending_action': 'awaiting_confirmation'}},
        {'query': '改选CTX-BLUE，规格CTX-BLUE-S1，仍然先记为待确认购买，现在不要下单。', 'state': {'product_id': 'CTX-BLUE', 'sku_id': 'CTX-BLUE-S1', 'pending_action': 'awaiting_confirmation'}},
    ]},
    {'id': 'cancel-after-compression', 'turns': [
        {'query': '预算90元，我选CTX-RED，规格CTX-RED-S1，先记为待确认购买，等我确认才下单。', 'state': {'price_max_major': 90, 'pending_action': 'awaiting_confirmation'}},
        {'query': '取消刚才待确认的购买，先不买了。只更新当前购买状态，不要创建订单。', 'state': {'pending_action': 'cancelled', 'price_max_major': 90}},
    ]},
    {'id': 'new-task-clears-state', 'turns': [
        {'query': '露营灯预算90元，我选CTX-RED，规格CTX-RED-S1，先记为待确认购买，不要下单。', 'state': {'price_max_major': 90, 'product_id': 'CTX-RED'}},
        {'query': '开始一个全新的购物任务，之前露营灯的预算、商品选择和待确认购买都清空。新需求和预算我稍后告诉你，现在只清空状态，不要搜索或下单。', 'absent': ['price_max_major', 'product_id', 'sku_id'], 'state': {'pending_action': 'none'}},
    ]},
    {'id': 'temporary-exception-expires', 'dislike': '不喜欢红色商品', 'turns': [
        {'query': '这一次可以接受红色，但不要删除长期偏好。帮我找寄中国的防水充电露营灯，预算150元，请搜索。', 'oracle_budget': 150, 'search': True, 'required': ['CTX-RED'], 'pref_count': 1},
        {'query': '继续找150元以内寄中国的防水充电露营灯，本轮不再有颜色例外，请按长期偏好重新搜索。', 'oracle_budget': 150, 'search': True, 'required': ['CTX-BLUE'], 'forbidden': ['CTX-RED', 'CTX-EXPENSIVE'], 'pref_count': 1},
    ]},
    {'id': 'delete-across-sessions', 'dislike': '不喜欢红色商品', 'turns': [
        {'query': '以后不用避开红色了，请删除我的这条长期偏好。', 'pref_count': 0},
        {'query': '帮我找寄中国的防水充电露营灯，150元以内，请搜索。', 'oracle_budget': 150, 'new_session': True, 'search': True, 'required': ['CTX-RED'], 'pref_count': 0},
    ]},
]


async def run(output):
    from app.infrastructure.settings import load_settings
    from app.domain.catalog.product import Product
    from app.domain.catalog.sku import Sku
    from app.domain.catalog.money import Money
    from app.domain.buyer.preference import BuyerPreference
    from app.infrastructure.persistence.persistent_product_repository import PersistentProductRepository
    from app.application.agents.orchestrator import SubmitIntentInput

    if urlsplit(load_settings().llm_base_url).hostname != 'api.deepseek.com':
        raise RuntimeError('Paid acceptance is authorized for DeepSeek only')
    if output.exists():
        raise ValueError('Choose a fresh output path')
    rec = Recorder(output)
    products = [Product(pid, '防水充电露营灯 '+color, 'Demo', '户外运动', 'CN', '便携 防水 充电 露营灯',
                        ships_to=['CN'], skus=[Sku(pid+'-S1', 'standard', Money.from_major_units(price, 'CNY'), 5)],
                        evidence={'colors': [color]})
                for pid, color, price in [('CTX-RED', 'red', 80), ('CTX-BLUE', 'blue', 80), ('CTX-EXPENSIVE', 'red', 220)]]
    write_json(output/'protocol.json', {'cases': CASES, 'products': [asdict(p) for p in products],
        'scope': '6 independent synthetic requirements, 2 turns each, real model/tools/SQLite; no web',
        'compression': 'forced once after first turn of each case; reload session before second turn',
        'budget_cap_cny': 1, 'api_call_cap': 64, 'permission_patch': False})
    write_json(output/'source-hashes.json', {str(p): digest(p) for p in Path('app').rglob('*') if p.suffix in ('.py', '.yml')})
    # Check fixture affordability before spending on model calls. This is an
    # oracle consistency check, not a model outcome or a relabelling of old runs.
    from app.application.usecases.product_recommendation import ProductRecommendationService
    from app.domain.catalog.product_search_spec import ProductSearchSpec
    oracle = ProductRecommendationService(PersistentProductRepository(output/'oracle', products=products))
    for case in CASES:
        for turn in case['turns']:
            if 'oracle_budget' not in turn:
                continue
            result = await oracle.execute(ProductSearchSpec('防水充电露营灯', ship_to='CN', price_max_major=turn['oracle_budget'], top_k=5))
            available = {h['product_id'] for h in result['hits']}
            if turn.get('search_empty'):
                assert not available, 'Fixture must have no affordable products'
            assert set(turn.get('required', [])) <= available, 'Required product exceeds fixture budget'
    rows = []
    with ApiCapture(rec, 64) as capture, SpendingGuard(capture, rec, limit=1), patch(
            'app.composition.PersistentProductRepository', lambda data_dir: PersistentProductRepository(data_dir, products=products)):
        async with container(rec, 'scenarios', consent=False) as c:
            for case in CASES:
                buyer = sid = case['id']
                if case.get('dislike'):
                    await c.orchestrator._preference_store.append(BuyerPreference(buyer, 'dislike', case['dislike']))
                for index, turn in enumerate(case['turns']):
                    if turn.get('new_session'):
                        sid += '-new'
                    q = c.bus.subscribe(sid)
                    TRIAL.set({'suite': 'context-scenarios', 'case_id': case['id'], 'turn': index})
                    print('BEGIN', case['id'], index, flush=True)
                    async with asyncio.timeout(150):
                        result = await c.orchestrator.handle_intent(SubmitIntentInput(sid, buyer, 'zh-CN', 'CNY', turn['query'], request_id=f'{case["id"]}-{index}'))
                    agent = await c.orchestrator._sessions.get_or_create(sid)
                    state = dict(agent.state.middle_context.get('current_shopping', {}))
                    events = drain(q)
                    c.bus.unsubscribe(sid, q)
                    searches = [e['payload'] for e in events if e['type'] == 'tool.result' and e['payload'].get('tool') == 'product_search_tool']
                    returned = {h['product_id'] for e in searches for h in e.get('hits', [])}
                    prefs = await c.orchestrator._preference_store.list_by_buyer(buyer)
                    db = database_snapshot(c.settings.data_dir/'globex.db')
                    checks = {'transport': result.success, 'no_orders': not db['orders']}
                    checks.update({f'state:{k}': state.get(k) == v for k, v in turn.get('state', {}).items()})
                    checks.update({f'absent:{k}': k not in state for k in turn.get('absent', [])})
                    if turn.get('search_empty'):
                        checks['search_empty'] = bool(searches) and not returned
                    if turn.get('search'):
                        checks['search_nonempty'] = bool(returned)
                    checks.update({f'required:{pid}': pid in returned for pid in turn.get('required', [])})
                    checks.update({f'forbidden:{pid}': pid not in returned for pid in turn.get('forbidden', [])})
                    if 'pref_count' in turn:
                        checks['preferences'] = len(prefs) == turn['pref_count']
                    row = {'case_id': case['id'], 'turn': index, 'checks': checks, 'passed': all(checks.values()),
                           'state': state, 'events': events, 'query': turn['query'], 'text': result.final_text,
                           'preferences': [asdict(p) for p in prefs], 'database': db}
                    rows.append(row)
                    rec.artifact(f'{case["id"]}-{index}.json', row)
                    print('END', case['id'], index, checks, flush=True)
                    if index == 0:
                        config = agent.context_config.model_copy(update={'trigger_ratio': .00001, 'reserve_ratio': .00001})
                        async with asyncio.timeout(120):
                            await agent.compress_context(context_config=config)
                        rec.artifact(f'{case["id"]}-compression.json', {'summary': agent.state.summary, 'state': agent.state.middle_context})
                    await c.orchestrator._sessions.persist(sid)
                    c.orchestrator._sessions._agents.pop(sid, None)
    write_json(output/'summary.json', {'independent_cases': len(CASES), 'turns': len(rows),
        'passed_turns': sum(r['passed'] for r in rows),
        'passed_cases': sum(all(r['passed'] for r in rows if r['case_id'] == case['id']) for case in CASES),
        'api_calls': len(capture.calls), 'api_errors': sum(bool(c['error']) for c in capture.calls),
        'failed_checks': [{'case': r['case_id'], 'turn': r['turn'], 'checks': [k for k,v in r['checks'].items() if not v]} for r in rows if not r['passed']]})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(run(parser.parse_args().output))
