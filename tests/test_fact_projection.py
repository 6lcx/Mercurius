"""Deduplicate model input without losing facts after compaction."""
import json
from copy import deepcopy
from types import SimpleNamespace
from agentscope.message import Msg, UserMsg, ToolResultBlock, ToolResultState
from agentscope.state import AgentState
from app.application.agents.critical_facts import CriticalFactsMiddleware


def test_visible_tool_evidence_is_not_repeated_but_survives_compaction():
    facts = {'hits': [{'product_id': 'P-A', 'sku_id': 'A1', 'price_major': 89,
                       'title': '灯', 'currency': 'CNY'}]}
    messages = [UserMsg('buyer', '找灯'), Msg(name='tool', role='assistant', content=[
        ToolResultBlock(id='call-1', name='product_search_tool', state=ToolResultState.SUCCESS,
                        output=json.dumps(facts, ensure_ascii=False))])]
    agent = SimpleNamespace(state=AgentState(context=messages))
    middleware = CriticalFactsMiddleware()
    projected = middleware._payload(agent, messages)
    assert not projected['historical_facts']
    assert not projected['known_entities']
    saved = deepcopy(agent.state.middle_context)
    agent.state.context = []
    after = middleware._payload(agent, [])
    assert after['historical_facts']['latest_result:product_search_tool'] == facts
    assert agent.state.middle_context == saved
    # The entity is already in historical_facts, so it needs only one copy.
    assert not after['known_entities']


def test_old_product_stays_available_when_latest_tool_result_changes():
    old = {'product_id': 'OLD', 'title': '旧商品', 'sku_id': 'OLD1'}
    new = {'product_id': 'NEW', 'title': '新商品', 'sku_id': 'NEW1'}
    agent = SimpleNamespace(state=AgentState(middle_context={
        'critical_facts': {'latest_result:product_search_tool': {'hits': [new]}},
        'critical_entities': {'product:OLD': old, 'product:NEW': new},
        'current_shopping': {'product_id': 'OLD', 'pending_action': 'cancelled'},
    }))
    payload = CriticalFactsMiddleware()._payload(agent, [])
    assert payload['known_entities'] == {'product:OLD': old}
    assert payload['current_shopping']['pending_action'] == 'cancelled'
    assert payload['historical_facts']['latest_result:product_search_tool']['hits'] == [new]
