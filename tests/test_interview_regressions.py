import asyncio
import pytest
from app.infrastructure.resilience import CircuitBreakerRegistry
from app.application.usecases.product_recommendation import ProductRecommendationService
from app.domain.catalog.product_search_spec import ProductSearchSpec

def test_half_open_only_one_probe():
    breaker = CircuitBreakerRegistry(failure_threshold=1, reset_seconds=1)
    breaker.record_failure('search', now=10)
    assert breaker.allow('search', now=12)
    assert not breaker.allow('search', now=12)
    breaker.record_success('search')
    assert breaker.allow('search', now=12)

@pytest.mark.asyncio
async def test_independent_discoveries_overlap():
    both_started = asyncio.Event()
    class Repo:
        async def list_all(self): return []
    class Discovery:
        count = 0
        async def discover(self, spec):
            self.count += 1
            if self.count == 2: both_started.set()
            await asyncio.wait_for(both_started.wait(), .3)
            return []
    discovery = Discovery()
    service = ProductRecommendationService(Repo(), discovery)
    results = await asyncio.gather(service.execute(ProductSearchSpec('tent')), service.execute(ProductSearchSpec('lamp')))
    assert all(r['discovery_status'] == 'discovered' for r in results)

@pytest.mark.asyncio
async def test_empty_preferences_inject_authoritative_reset_after_restart():
    from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
    from app.infrastructure.eventbus import TradeEventBus
    class Store:
        async def list_by_buyer(self, buyer): return []
    orchestrator = MainAgentOrchestrator(None, TradeEventBus(), Store())
    inputs = await orchestrator._build_inputs(SubmitIntentInput('s', 'b', 'zh', 'CNY', '继续'), 's')
    assert len(inputs) == 2
    assert 'preferences-empty' in str(inputs[0].content)
    assert 'summary' in str(inputs[0].content)

@pytest.mark.asyncio
async def test_preference_read_failure_is_not_erasure():
    from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
    from app.infrastructure.eventbus import TradeEventBus
    class Store:
        async def list_by_buyer(self, buyer): raise OSError('offline')
    orchestrator = MainAgentOrchestrator(None, TradeEventBus(), Store())
    inputs = await orchestrator._build_inputs(SubmitIntentInput('s', 'b', 'zh', 'CNY', '继续'), 's')
    assert 'preference-read-failed' in str(inputs[0].content)
    assert 'preferences-empty' not in str(inputs[0].content)

@pytest.mark.asyncio
async def test_shared_half_open_single_probe():
    from tests.test_harness_infra import FakeCache
    from app.infrastructure.shared_breaker import SharedCircuitBreakerRegistry
    cache = FakeCache()
    a = SharedCircuitBreakerRegistry(cache, failure_threshold=1, reset_seconds=1)
    b = SharedCircuitBreakerRegistry(cache, failure_threshold=1, reset_seconds=1)
    await a.record_failure_async('t', now=10)
    results = await asyncio.gather(a.allow_async('t', now=12), b.allow_async('t', now=12))
    assert sum(results) == 1

@pytest.mark.asyncio
async def test_material_exclusion_blocks_match_and_marks_unknown():
    from tests.test_product_recommendation import Repo, product
    plastic = product('plastic', evidence={'materials': ['plastic']})
    metal = product('metal', evidence={'materials': ['aluminum']})
    unknown = product('unknown')
    service = ProductRecommendationService(Repo([plastic, metal, unknown]))
    result = await service.execute(ProductSearchSpec('\u9732\u8425\u706f', excluded_materials=('plastic',)))
    assert [hit['product_id'] for hit in result['hits']] == ['metal']
    assert any(hit['product_id'] == 'plastic' for hit in result['filtered_out'])
    assert any(hit['product_id'] == 'unknown' for hit in result['unverified_candidates'])

@pytest.mark.asyncio
async def test_stored_material_exclusion_enforced_without_model_and_deleted_immediately():
    from tests.test_product_recommendation import Repo, product
    from app.domain.buyer.preference import BuyerPreference
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    class Store:
        values = [BuyerPreference(buyer_id='b', kind='dislike', statement='\u4e0d\u8981\u5851\u6599\u6750\u8d28')]
        async def list_by_buyer(self, buyer): return self.values
    store = Store()
    service = ProductRecommendationService(Repo([product('plastic', evidence={'materials': ['plastic']})]), preference_store=store)
    token = ShoppingContext.set(ShoppingContextSnapshot('s', 'b', 'zh', 'CNY'))
    try:
        spec = ProductSearchSpec('\u9732\u8425\u706f', top_k=1)
        assert not (await service.execute(spec))['hits']
        store.values = []
        assert (await service.execute(spec))['hits']
    finally:
        ShoppingContext.reset(token)

@pytest.mark.asyncio
async def test_compression_keeps_exact_tool_facts_outside_model_summary():
    from types import SimpleNamespace
    from agentscope.state import AgentState
    from agentscope.message import Msg, ToolResultBlock, ToolResultState
    from app.application.agents.critical_facts import CriticalFactsMiddleware
    import json
    state = AgentState(context=[Msg(name='tool', role='assistant', content=[ToolResultBlock(
        id='call-1', name='product_search_tool', state=ToolResultState.SUCCESS,
        output=json.dumps({'hits': [{'product_id': 'SKU-00042', 'price_major': 399.12, 'currency': 'CNY'}]}))])])
    agent = SimpleNamespace(state=state)
    async def compress(**kwargs):
        state.context = []
        state.summary = 'model omitted every identifier'
    middleware = CriticalFactsMiddleware()
    await middleware.on_compress_context(agent, {}, compress)
    restored = SimpleNamespace(state=AgentState.model_validate_json(state.model_dump_json()))
    async def capture(**kwargs):
        return kwargs['messages']
    messages = await middleware.on_model_call(restored, {'messages': []}, capture)
    rendered = '\n'.join(message.get_text_content() for message in messages)
    assert 'SKU-00042' in rendered and '399.12' in rendered

@pytest.mark.asyncio
async def test_shared_probe_with_real_redis(tmp_path):
    import subprocess, socket, shutil
    from app.infrastructure.cache.redis_cache import RedisCache
    from app.infrastructure.shared_breaker import SharedCircuitBreakerRegistry
    import os, uuid
    endpoint = os.environ.get('GLOBEX_TEST_REDIS_URL')
    if endpoint:
        cache = RedisCache(endpoint)
        tool_name = 'integration-' + uuid.uuid4().hex
        try:
            assert await cache.ping()
            registries = [SharedCircuitBreakerRegistry(cache, failure_threshold=1, reset_seconds=1) for _ in range(20)]
            await registries[0].record_failure_async(tool_name, now=10)
            allowed = await asyncio.gather(*(r.allow_async(tool_name, now=12) for r in registries))
            assert sum(allowed) == 1
        finally:
            await cache.delete('globex:breaker:' + tool_name)
            await cache.delete('globex:breaker:' + tool_name + ':probe')
            await cache.close()
        return
    executable = shutil.which('redis-server')
    if not executable: pytest.skip('redis-server unavailable')
    version = subprocess.check_output([executable, '--version'], text=True)
    import re
    match = re.search(r'v=(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (6, 2):
        pytest.skip('Redis >= 6.2 required for supported integration environment')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    process = subprocess.Popen([executable, '--bind', '127.0.0.1', '--port', str(port),
                                '--save', '', '--appendonly', 'no', '--dir', str(tmp_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    cache = RedisCache(f'redis://127.0.0.1:{port}/0')
    try:
        for _ in range(100):
            if await cache.ping(): break
            await asyncio.sleep(.02)
        else: pytest.fail('isolated Redis did not start')
        registries = [SharedCircuitBreakerRegistry(cache, failure_threshold=1, reset_seconds=1) for _ in range(20)]
        await registries[0].record_failure_async('test', now=10)
        allowed = await asyncio.gather(*(r.allow_async('test', now=12) for r in registries))
        assert sum(allowed) == 1
    finally:
        await cache.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

@pytest.mark.asyncio
async def test_old_completion_cannot_release_another_tasks_probe():
    from tests.test_harness_infra import FakeCache
    from app.infrastructure.shared_breaker import SharedCircuitBreakerRegistry
    cache = FakeCache()
    breaker = SharedCircuitBreakerRegistry(cache, failure_threshold=1, reset_seconds=1)
    await breaker.record_failure_async('t', now=10)
    acquired, finish = asyncio.Event(), asyncio.Event()
    async def probe():
        assert await breaker.allow_async('t', now=12)
        acquired.set()
        await finish.wait()
        await breaker.record_success_async('t')
    task = asyncio.create_task(probe())
    await acquired.wait()
    await breaker.record_success_async('t')
    assert not await breaker.allow_async('t', now=12)
    finish.set()
    await task
    assert await breaker.status_async('t') == 'closed'
