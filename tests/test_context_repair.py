"""Business outcomes for context repair; no paid APIs or fabricated LLM scores."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.message import UserMsg, ToolResultState
from agentscope.state import AgentState

from app.application.agents.critical_facts import CriticalFactsMiddleware
from app.application.tools.shopping_context_tool import build_allow_preference_once_tool, build_update_shopping_context_tool
from app.application.tools.forget_preference_tool import build_forget_preference_tool
from app.application.tools.order_tools import build_create_order_tool
from app.application.usecases.product_recommendation import ProductRecommendationService
from app.domain.buyer.preference import BuyerPreference
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.persistence.json_file_stores import JsonFilePreferenceStore
from tests.test_product_recommendation import Repo, product


def snapshot(query='', state=None, buyer='buyer', session='s'):
    return ShoppingContextSnapshot(session, buyer, 'zh-CN', 'CNY', raw_query=query, session_data=state)


async def seeded(tmp_path, statement='不喜欢红色商品'):
    store = JsonFilePreferenceStore(tmp_path)
    await store.append(BuyerPreference('buyer', 'dislike', statement))
    return store


def ids(result):
    return [row['product_id'] for row in result['hits']]


@pytest.mark.asyncio
async def test_color_exception_changes_real_filter_only_for_current_turn(tmp_path):
    store = await seeded(tmp_path)
    service = ProductRecommendationService(Repo([product('red', evidence={'colors': ['red']}),
                                                product('blue', evidence={'colors': ['blue']})]), preference_store=store)
    spec = ProductSearchSpec('露营灯', top_k=2)
    token = ShoppingContext.set(snapshot('这次可以接受红色，但不要删除长期偏好'))
    try:
        assert ids(await service.execute(spec)) == ['blue']
        reply = await build_allow_preference_once_tool(store)('不喜欢红色商品', '这次可以接受红色')
        assert reply.state == ToolResultState.SUCCESS
        assert set(ids(await service.execute(spec))) == {'red', 'blue'}
        assert len(await store.list_by_buyer('buyer')) == 1
    finally:
        ShoppingContext.reset(token)
    # New user turn, including in the same session: the exception has expired.
    token = ShoppingContext.set(snapshot('再推荐一下'))
    try:
        assert ids(await service.execute(spec)) == ['blue']
    finally:
        ShoppingContext.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('query,evidence', [
    ('找露营灯', '这次可以接受红色'),
    ('这次不可以接受红色', '这次不可以接受红色'),
    ('这次可以接受红色吗？', '这次可以接受红色吗？'),
    ('这次可以接受蓝色', '这次可以接受蓝色'),
])
async def test_unapproved_or_unrelated_exception_is_rejected(tmp_path, query, evidence):
    store = await seeded(tmp_path)
    ctx = snapshot(query)
    token = ShoppingContext.set(ctx)
    try:
        result = await build_allow_preference_once_tool(store)('不喜欢红色商品', evidence)
        assert result.state == ToolResultState.ERROR
        assert not ctx.turn_data.get('preference_exceptions')
    finally:
        ShoppingContext.reset(token)


@pytest.mark.asyncio
async def test_exception_inherited_by_async_worker_but_not_other_buyer(tmp_path):
    store = await seeded(tmp_path)
    await store.append(BuyerPreference('other', 'dislike', '不喜欢红色商品'))
    service = ProductRecommendationService(Repo([product('red', evidence={'colors': ['red']})]), preference_store=store)
    async def other():
        token = ShoppingContext.set(snapshot(buyer='other', session='other'))
        try:
            return ids(await service.execute(ProductSearchSpec('露营灯')))
        finally:
            ShoppingContext.reset(token)
    token = ShoppingContext.set(snapshot('这次可以接受红色'))
    try:
        await build_allow_preference_once_tool(store)('不喜欢红色商品', '这次可以接受红色')
        mine, theirs = await asyncio.gather(service.execute(ProductSearchSpec('露营灯')), other())
        assert ids(mine) == ['red'] and theirs == []
    finally:
        ShoppingContext.reset(token)


@pytest.mark.asyncio
async def test_deletion_rechecks_cached_products_and_survives_reopening(tmp_path):
    store = await seeded(tmp_path)
    repo = Repo([product('red', evidence={'colors': ['red']})])
    service = ProductRecommendationService(repo, preference_store=store, cache_path=tmp_path/'recommendations.json')
    ctx = snapshot('以后不用避开红色了', {})
    token = ShoppingContext.set(ctx)
    try:
        assert ids(await service.execute(ProductSearchSpec('露营灯'))) == []
        reply = await build_forget_preference_tool(store, TradeEventBus())('不喜欢红色商品')
        assert reply.state == ToolResultState.SUCCESS
        assert ids(await service.execute(ProductSearchSpec('露营灯'))) == ['red']
        assert ctx.session_data['withdrawn_preferences'] == ['不喜欢红色商品']
    finally:
        ShoppingContext.reset(token)
    reopened = JsonFilePreferenceStore(tmp_path)
    token = ShoppingContext.set(snapshot(session='new-session'))
    try:
        assert await reopened.list_by_buyer('buyer') == []
        assert ids(await ProductRecommendationService(repo, preference_store=reopened).execute(ProductSearchSpec('露营灯'))) == ['red']
    finally:
        ShoppingContext.reset(token)


@pytest.mark.asyncio
async def test_current_budget_survives_repeated_compaction_and_overrides_stale_tool_args():
    agent = SimpleNamespace(state=AgentState(summary='预算500元'))
    middleware = CriticalFactsMiddleware()
    token = ShoppingContext.set(snapshot('预算改成150元', agent.state.middle_context))
    try:
        reply = await build_update_shopping_context_tool()('预算改成150元', price_max_major=150)
        assert reply.state == ToolResultState.SUCCESS
        async def compress(**kwargs):
            agent.state.context = []
            agent.state.summary = '错误旧摘要：预算500元'
        for _ in range(3):
            await middleware.on_compress_context(agent, {}, compress)
            agent.state = AgentState.model_validate_json(agent.state.model_dump_json())
    finally:
        ShoppingContext.reset(token)
    token = ShoppingContext.set(snapshot('继续', agent.state.middle_context))
    try:
        service = ProductRecommendationService(Repo([product('cheap', price=80), product('expensive', price=250)]))
        result = await service.execute(ProductSearchSpec('露营灯', ship_to='CN', price_max_major=500))
        assert ids(result) == ['cheap']
        assert any(r['product_id'] == 'expensive' and r['reason'] == 'over_price_cap' for r in result['filtered_out'])
    finally:
        ShoppingContext.reset(token)


@pytest.mark.asyncio
async def test_cancelled_purchase_does_not_reach_order_usecase_after_restart():
    agent = SimpleNamespace(state=AgentState())
    token = ShoppingContext.set(snapshot('取消待确认购买，先不买了', agent.state.middle_context))
    try:
        await build_update_shopping_context_tool()('取消待确认购买，先不买了', pending_action='cancelled')
    finally:
        ShoppingContext.reset(token)
    restored = AgentState.model_validate_json(agent.state.model_dump_json())
    class MustNotExecute:
        async def execute(self, **kwargs):
            raise AssertionError('cancelled purchase reached order service')
    token = ShoppingContext.set(snapshot('继续聊聊', restored.middle_context))
    try:
        reply = await build_create_order_tool(MustNotExecute(), TradeEventBus())(
            [{'product_id': 'old', 'sku_id': 'old-sku'}], {})
        assert reply.state == ToolResultState.ERROR
    finally:
        ShoppingContext.reset(token)


@pytest.mark.asyncio
async def test_product_switch_invalidates_old_sku_and_new_task_clears_budget():
    state = {'current_shopping': {'product_id': 'A', 'sku_id': 'A1', 'price_max_major': 300,
                                  'pending_action': 'awaiting_confirmation'}}
    token = ShoppingContext.set(snapshot('改选B，另外开始一个新任务', state))
    try:
        tool = build_update_shopping_context_tool()
        await tool('改选B', product_id='B')
        assert state['current_shopping']['pending_action'] == 'none'
        assert 'sku_id' not in state['current_shopping']
        await tool('另外开始一个新任务', new_task=True)
        assert 'price_max_major' not in state['current_shopping']
        assert 'product_id' not in state['current_shopping']
    finally:
        ShoppingContext.reset(token)


@pytest.mark.asyncio
async def test_stable_system_prefix_and_latest_preference_projection():
    agent = SimpleNamespace(state=AgentState())
    middleware = CriticalFactsMiddleware()
    before = await middleware.on_system_prompt(agent, 'base')
    agent.state.middle_context['current_shopping'] = {'price_max_major': 150}
    after = await middleware.on_system_prompt(agent, 'base')
    assert before == after
    messages = [UserMsg('memory_hint', 'OLD preference'), UserMsg('buyer', 'hello'),
                UserMsg('memory_hint', 'NEW preference'), UserMsg('buyer', 'continue')]
    async def capture(**kwargs):
        return kwargs['messages']
    projected = await middleware.on_model_call(agent, {'messages': messages}, capture)
    text = '\n'.join(m.get_text_content() for m in projected)
    assert 'OLD preference' not in text and 'NEW preference' in text and '150' in text
    assert len(messages) == 4  # Historical messages were not destructively rewritten.


@pytest.mark.asyncio
async def test_deleted_hint_replaced_on_next_model_call_same_turn(tmp_path):
    store = await seeded(tmp_path)
    token = ShoppingContext.set(snapshot('删除红色偏好', {}))
    try:
        await build_forget_preference_tool(store, TradeEventBus())('不喜欢红色商品')
        async def capture(**kwargs):
            return kwargs['messages']
        messages = await CriticalFactsMiddleware().on_model_call(SimpleNamespace(state=AgentState()),
            {'messages': [UserMsg('memory_hint', 'STALE dislike')]}, capture)
        assert 'STALE dislike' not in '\n'.join(m.get_text_content() for m in messages)
        assert any('Refreshed after deletion' in m.get_text_content() for m in messages)
    finally:
        ShoppingContext.reset(token)


def test_new_tools_have_valid_agentscope_schemas():
    from agentscope.tool import FunctionTool
    tools = [FunctionTool(build_update_shopping_context_tool()), FunctionTool(build_allow_preference_once_tool(None))]
    assert len(tools) == 2


@pytest.mark.asyncio
async def test_budget_value_must_appear_in_current_user_quote():
    state = {}
    token = ShoppingContext.set(snapshot('预算改成150元', state))
    try:
        result = await build_update_shopping_context_tool()('预算改成150元', price_max_major=500)
        assert result.state == ToolResultState.ERROR
        assert not state
    finally:
        ShoppingContext.reset(token)


@pytest.mark.asyncio
async def test_fresh_preferences_survive_when_compression_removes_all_hints():
    ctx = snapshot()
    ctx.turn_data['preference_hint'] = 'CURRENT dislikes remain authoritative'
    token = ShoppingContext.set(ctx)
    try:
        async def capture(**kwargs):
            return kwargs['messages']
        messages = await CriticalFactsMiddleware().on_model_call(SimpleNamespace(state=AgentState(summary='old preferences')),
            {'messages': []}, capture)
        assert any('CURRENT dislikes' in m.get_text_content() for m in messages)
    finally:
        ShoppingContext.reset(token)
