import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.domain.queue.ports.task_queue import IntentTask
from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue, _LARGE_STREAM

@pytest.mark.asyncio
async def test_consumer_recovers_large_stream_pending_and_acknowledges_source():
    intent = IntentTask('task', 'session', 'buyer', 'zh-CN', 'CNY', 'query')
    stopped = False
    claims = []
    async def claim(stream, *args, **kwargs):
        claims.append(stream)
        return ['0-0', [('1-0', {b'payload': json.dumps(intent.to_dict()).encode()})], []] if stream == _LARGE_STREAM else ['0-0', [], []]
    async def read(*args, **kwargs):
        await asyncio.sleep(0.01)
        return []
    client = NS(xgroup_create=AsyncMock(), xautoclaim=claim, xack=AsyncMock(), xreadgroup=read)
    async def handler(task):
        nonlocal stopped
        assert task.task_id == 'task'
        stopped = True
    queue = RedisStreamTaskQueue(client)
    await asyncio.wait_for(queue.consume('worker', handler, lambda: stopped, block_ms=1), 0.3)
    assert _LARGE_STREAM in claims
    client.xack.assert_awaited_once_with(_LARGE_STREAM, 'globex-workers', '1-0')

@pytest.mark.asyncio
async def test_worker_uses_structured_failure_and_propagates_trace():
    from app.worker import process_task
    from app.infrastructure.context import ShoppingContext
    from app.application.agents.orchestrator import SubmitIntentOutput
    observed = []
    async def handle(intent):
        observed.append((intent.request_id, intent.traceparent))
        return SubmitIntentOutput('session', '[error] unavailable', success=False, error='unavailable')
    container = NS(task_queue=NS(set_status=AsyncMock()), bus=NS(publish=lambda *a: None), orchestrator=NS(handle_intent=handle))
    task = IntentTask('task', 'session', 'buyer', 'zh-CN', 'CNY', 'query', traceparent='00-'+'1'*32+'-'+'2'*16+'-01')
    await process_task(container, task)
    assert container.task_queue.set_status.await_args.args[0].state == 'failed'
    assert observed == [('task', task.traceparent)]


@pytest.mark.asyncio
async def test_order_replay_across_repository_instances_is_atomic(tmp_path):
    from app.infrastructure.persistence.sql.repositories import create_engine, bootstrap_schema, SqlOrderRepository
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from app.application.usecases.order_usecases import PlaceOrderUseCase, CancelOrderUseCase, OrderItemInput
    from app.domain.order.address import Address
    engine = create_engine('sqlite+aiosqlite:///' + str(tmp_path/'orders.db'))
    await bootstrap_schema(engine)
    try:
        products1, products2 = InMemoryProductRepository(), InMemoryProductRepository()
        p = (await products1.list_all())[0]
        initial = p.skus[0].stock
        item = OrderItemInput(p.product_id, p.skus[0].sku_id, 1)
        address = Address('buyer','CN','','city','road','','123')
        first = PlaceOrderUseCase(products1, SqlOrderRepository(engine))
        second = PlaceOrderUseCase(products2, SqlOrderRepository(engine))
        results = await asyncio.gather(first.execute('buyer',[item],address,idempotency_key='request'), second.execute('buyer',[item],address,idempotency_key='request'))
        assert results[0]['order_id'] == results[1]['order_id']
        from sqlalchemy import text
        async with engine.connect() as conn:
            assert (await conn.scalar(text('SELECT COUNT(*) FROM orders'))) == 1
            assert (await conn.scalar(text('SELECT stock FROM order_inventory'))) == initial - 1
        with pytest.raises(ValueError, match='幂等'):
            await second.execute('buyer',[OrderItemInput(item.product_id,item.sku_id,2)],address,idempotency_key='request')
        cancel1, cancel2 = CancelOrderUseCase(products1,SqlOrderRepository(engine)), CancelOrderUseCase(products2,SqlOrderRepository(engine))
        await asyncio.gather(cancel1.execute(results[0]['order_id'],'test'),cancel2.execute(results[0]['order_id'],'test'))
        async with engine.connect() as conn:
            assert (await conn.scalar(text('SELECT stock FROM order_inventory'))) == initial
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_sql_inventory_cannot_oversell_and_failed_transaction_can_retry(tmp_path):
    from app.infrastructure.persistence.sql.repositories import create_engine, bootstrap_schema, SqlOrderRepository
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from app.application.usecases.order_usecases import PlaceOrderUseCase, OrderItemInput
    from app.domain.order.address import Address
    from sqlalchemy import text
    engine = create_engine('sqlite+aiosqlite:///' + str(tmp_path/'stock.db'))
    await bootstrap_schema(engine)
    products = InMemoryProductRepository()
    p = (await products.list_all())[0]
    initial = p.skus[0].stock
    address = Address('buyer','CN','','city','road','','123')
    repos = [SqlOrderRepository(engine), SqlOrderRepository(engine)]
    try:
        cases = [PlaceOrderUseCase(products, repo) for repo in repos]
        item = OrderItemInput(p.product_id,p.skus[0].sku_id, initial)
        results = await asyncio.gather(*(case.execute('buyer',[item],address,idempotency_key=f'full-{i}') for i,case in enumerate(cases)), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        async with engine.connect() as conn:
            assert await conn.scalar(text('SELECT stock FROM order_inventory')) == 0
            assert await conn.scalar(text('SELECT COUNT(*) FROM order_requests')) == 1
        # Closing/reopening the engine must preserve the successful replay.
        winner = next(i for i,result in enumerate(results) if isinstance(result, dict))
        await engine.dispose()
        reopened = create_engine('sqlite+aiosqlite:///' + str(tmp_path/'stock.db'))
        try:
            replay = await PlaceOrderUseCase(InMemoryProductRepository(),SqlOrderRepository(reopened)).execute('buyer',[item],address,idempotency_key=f'full-{winner}')
            assert replay['order_id'] == results[winner]['order_id']
        finally:
            await reopened.dispose()
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_failed_order_save_rolls_back_inventory_and_request_record(tmp_path, monkeypatch):
    from app.infrastructure.persistence.sql.repositories import create_engine, bootstrap_schema, SqlOrderRepository
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from app.application.usecases.order_usecases import PlaceOrderUseCase, OrderItemInput
    from app.domain.order.address import Address
    from sqlalchemy import text
    engine = create_engine('sqlite+aiosqlite:///' + str(tmp_path/'failure.db'))
    await bootstrap_schema(engine)
    products = InMemoryProductRepository()
    p = (await products.list_all())[0]
    repo = SqlOrderRepository(engine)
    case = PlaceOrderUseCase(products,repo)
    item=OrderItemInput(p.product_id,p.skus[0].sku_id,1)
    address=Address('buyer','CN','','city','road','','123')
    write = repo._write_order
    monkeypatch.setattr(repo,'_write_order',AsyncMock(side_effect=RuntimeError('storage down')))
    try:
        with pytest.raises(RuntimeError):
            await case.execute('buyer',[item],address,idempotency_key='retry')
        async with engine.connect() as conn:
            assert await conn.scalar(text('SELECT COUNT(*) FROM order_requests')) == 0
            assert await conn.scalar(text('SELECT COUNT(*) FROM order_inventory')) == 0
        monkeypatch.setattr(repo,'_write_order',write)
        assert (await case.execute('buyer',[item],address,idempotency_key='retry'))['order_id']
    finally:
        await engine.dispose()

def test_trace_survives_task_and_backplane_event_roundtrips():
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    from app.infrastructure.eventbus import TradeEventBus, TradeEvent
    from app.presentation.server import _traceparent
    trace = _traceparent('invalid')
    task=IntentTask('task','session','buyer','zh-CN','CNY','query',traceparent=trace)
    assert IntentTask.from_dict(task.to_dict()).traceparent == trace
    bus=TradeEventBus()
    receiver=bus.subscribe('session')
    token=ShoppingContext.set(ShoppingContextSnapshot('session','buyer','zh-CN','CNY','task',trace))
    try:
        bus.publish('session','task.started',{})
        event=TradeEvent.from_dict(receiver.get_nowait().to_dict())
        assert (event.request_id,event.traceparent) == ('task',trace)
    finally:
        ShoppingContext.reset(token)
    assert ShoppingContext.current() is None

@pytest.mark.asyncio
async def test_catalog_reads_shared_stock_without_mutating_seed_baseline(tmp_path):
    from app.infrastructure.persistence.sql.repositories import create_engine, bootstrap_schema, SqlOrderRepository
    from app.infrastructure.persistence.persistent_product_repository import PersistentProductRepository
    from app.application.usecases.order_usecases import PlaceOrderUseCase, OrderItemInput, CancelOrderUseCase
    from app.domain.order.address import Address
    engine = create_engine('sqlite+aiosqlite:///' + str(tmp_path/'view.db'))
    await bootstrap_schema(engine)
    repo=SqlOrderRepository(engine)
    catalog=PersistentProductRepository(tmp_path)
    catalog.set_inventory_reader(repo.inventory_for)
    try:
        product=(await catalog.list_all())[0]
        sku=product.skus[0]
        initial=sku.stock
        result=await PlaceOrderUseCase(catalog,repo).execute('buyer',[OrderItemInput(product.product_id,sku.sku_id,1)],Address('buyer','CN','','city','road','','123'),idempotency_key='view')
        assert (await catalog.find_by_id(product.product_id)).skus[0].stock == initial-1
        assert product.skus[0].stock == initial
        await CancelOrderUseCase(catalog,repo).execute(result['order_id'],'test')
        assert (await catalog.find_by_ids([product.product_id]))[0].skus[0].stock == initial
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_orchestrator_attaches_remote_trace_and_reports_failure():
    from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
    from app.infrastructure.eventbus import TradeEventBus
    from app.infrastructure.context import ShoppingContext
    from opentelemetry import trace
    seen=[]
    async def fail(_):
        seen.append(trace.get_current_span().get_span_context().trace_id)
        raise RuntimeError('offline failure')
    sessions=NS(get_or_create=fail,persist=AsyncMock())
    orchestrator=MainAgentOrchestrator(sessions,TradeEventBus(),None)
    parent='00-'+'1'*32+'-'+'2'*16+'-01'
    output=await orchestrator.handle_intent(SubmitIntentInput('session','buyer','zh-CN','CNY','query',traceparent=parent))
    assert output.success is False and output.error == 'offline failure'
    assert seen == [int('1'*32,16)]
    assert ShoppingContext.current() is None
    assert trace.get_current_span().get_span_context().trace_id != int('1'*32,16)
