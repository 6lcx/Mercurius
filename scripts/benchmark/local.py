"""Actual local services; only explicitly labelled upstream fault injection is synthetic."""
from __future__ import annotations
import asyncio
import json
import time
from dataclasses import asdict

from .common import ROOT, pairs, safe_error, write_json


async def wait_monotonic(seconds):
    """Wait on the circuit's clock, which can be coarse on Windows/Python 3.12.

    asyncio's timer may wake while time.monotonic has not advanced yet.
    A single 12ms sleep is therefore not proof that a 10ms cooldown elapsed.
    """
    started = time.monotonic()
    deadline = started + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        await asyncio.sleep(remaining)
    return time.monotonic() - started


async def retrieval(rec, repeats=3):
    from app.application.usecases.product_recommendation import ProductRecommendationService
    from app.domain.catalog.product_search_spec import ProductSearchSpec
    from app.infrastructure.embedding.local_embedding import LocalEmbeddingClient
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from scripts.eval.metrics import recall_at_k, mrr, ndcg_at_k
    cases = [json.loads(s) for s in (ROOT / 'eval/product_recall.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
    for index, case in enumerate(cases):
        case['id'] = f'q{index:03}'
    write_json(rec.output / 'retrieval-cases.json', cases)
    embedder = LocalEmbeddingClient()
    start = time.perf_counter()
    await embedder.embed('模型预加载，用时不混入稳态查询。')
    rec.append('setup.jsonl', dict(operation='load_local_embedding', elapsed_s=time.perf_counter()-start))
    for repeat, case, variant in pairs(cases, repeats, ('rules', 'rules_embedding')):
        repo = InMemoryProductRepository()
        svc = ProductRecommendationService(repo, embedder=embedder if variant == 'rules_embedding' else None)
        spec = ProductSearchSpec(case['query'], top_k=8, ship_to=case.get('ship_to'), price_max_major=case.get('price_max_major'))
        started = time.perf_counter()
        error, payload = None, {}
        try:
            payload = await svc.execute(spec)
        except Exception as exc:
            error = safe_error(exc)
        elapsed = time.perf_counter()-started
        ids = [r['product_id'] for r in payload.get('hits', [])]
        products = {p.product_id: p for p in await repo.list_all()}
        violations = []
        for hit in payload.get('hits', []):
            product = products[hit['product_id']]
            if spec.ship_to and product.ships_to and spec.ship_to not in product.ships_to:
                violations.append('destination:' + product.product_id)
            if spec.price_max_major is not None:
                amount = hit.get('landed_price', {}).get('landed_total_major')
                # Unknown landed price must not be presented as a verified hit under a cap.
                if amount is None or amount > spec.price_max_major:
                    violations.append('budget:' + product.product_id)
        score = recall_at_k(ids, case['relevant'], 8)
        artifact = rec.artifact(f'retrieval-{case["id"]}-{repeat}-{variant}.json', payload)
        rec.result('retrieval', case['id'], variant, repeat, elapsed,
                   not error and score > 0 and not violations, kind=case.get('kind', 'lexical'),
                   recall=score, mrr=mrr(ids, case['relevant']), ndcg=ndcg_at_k(ids, case['relevant'], 8),
                   query=case['query'], retrieved=ids, relevant=case['relevant'],
                   constraint_violations=violations, actual_strategy=payload.get('recall_strategy'),
                   rerank_applied=payload.get('rerank_applied'), error=error, artifact=artifact,
                   mock=False, dataset_scope='existing_development_labels')


async def memory(rec, repeats=3):
    from app.infrastructure.persistence.sql.repositories import create_engine, bootstrap_schema, SqlPreferenceStore
    from app.application.memory.preference_selector import PreferenceSelector, render_preference_hint
    from app.domain.buyer.preference import BuyerPreference
    from app.application.agents.orchestrator import MainAgentOrchestrator, SubmitIntentInput
    from app.infrastructure.eventbus import TradeEventBus
    cases = [dict(id=f'likes{likes}-dislikes{dislikes}', likes=likes, dislikes=dislikes)
             for likes in (0, 3, 8, 25) for dislikes in (0, 1, 7)]
    write_json(rec.output / 'memory-cases.json', cases)
    for repeat, case, variant in pairs(cases, repeats, ('all_preferences', 'selected_preferences')):
        path = rec.output / 'state' / f'memory-{case["id"]}-{repeat}-{variant}.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f'sqlite+aiosqlite:///{path.as_posix()}')
        started = time.perf_counter()
        try:
            await bootstrap_schema(engine)
            store = SqlPreferenceStore(engine)
            items = [BuyerPreference('buyer-A', kind, f'{"喜欢" if kind == "like" else "排除"}选项{i}', f'2026-01-{i+1:02}T00:00:00+00:00')
                     for kind, n in (('like', case['likes']), ('dislike', case['dislikes'])) for i in range(n)]
            for item in items:
                await store.append(item)
            await store.append(BuyerPreference('buyer-B', 'dislike', '其他买家的排除条件'))
            await engine.dispose()
            engine = create_engine(f'sqlite+aiosqlite:///{path.as_posix()}')
            store = SqlPreferenceStore(engine)
            restored = await store.list_by_buyer('buyer-A')
            selected = restored if variant == 'all_preferences' else await PreferenceSelector().select(restored, '旅行用品', 5)
            expected_dislikes = {p.statement for p in items if p.kind == 'dislike'}
            actual_dislikes = {p.statement for p in selected if p.kind == 'dislike'}
            hints = render_preference_hint(selected)
            # Both variants share real persistence/deletion verification.
            for item in items:
                await store.delete('buyer-A', item.statement)
            orch = MainAgentOrchestrator(None, TradeEventBus(), store)
            inputs = await orch._build_inputs(SubmitIntentInput('new-session', 'buyer-A', 'zh-CN', 'CNY', '继续'), 'new-session')
            deletion_ok = not await store.list_by_buyer('buyer-A') and 'preferences-empty' in inputs[0].get_text_content()
            other_ok = len(await store.list_by_buyer('buyer-B')) == 1
            success = (len(restored) == len(items) and actual_dislikes == expected_dislikes and deletion_ok and other_ok)
            artifact = rec.artifact(f'memory-{case["id"]}-{repeat}-{variant}.json',
                                    dict(original=[asdict(p) for p in items], restored=[asdict(p) for p in restored],
                                         selected=[asdict(p) for p in selected], after_delete=[m.model_dump(mode='json') for m in inputs]))
            rec.result('memory', case['id'], variant, repeat, time.perf_counter()-started, success,
                       input_chars=len(hints), selected_count=len(selected), original_count=len(items),
                       dislike_retention=len(actual_dislikes & expected_dislikes)/len(expected_dislikes) if expected_dislikes else None,
                       persisted=len(restored) == len(items), deleted=deletion_ok, buyer_isolation=other_ok,
                       artifact=artifact, mock=False)
        finally:
            await engine.dispose()


async def faults(rec, repeats=10):
    from agentscope.credential import OpenAICredential
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import ToolChunk
    from app.infrastructure.llm import ThrottledChatModel
    from app.infrastructure.throttle import GatewayThrottle
    from app.infrastructure.eventbus import TradeEventBus
    from app.infrastructure.resilience import ToolResilienceMiddleware, CircuitBreakerRegistry
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    from types import SimpleNamespace

    class ExternalFaultModel(ThrottledChatModel):
        def __init__(self, scenario, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.scenario, self.calls = scenario, 0

        async def _invoke_upstream(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(.002)
            if self.scenario == 'permanent':
                raise ValueError('invalid request')
            if self.scenario == 'persistent_transient' or self.scenario == 'transient_once' and self.calls == 1:
                raise RuntimeError('429 rate limit (injected)')
            async def stream():
                if self.scenario == 'pre_stream_once' and self.calls == 1:
                    raise RuntimeError('connection reset (injected)')
                yield 'first'
                if self.scenario == 'partial_stream':
                    raise RuntimeError('connection reset (injected)')
                yield 'last'
            return stream()

    class ExternalFallback:
        model = 'injected-fallback'
        def __init__(self):
            self.calls = 0
        async def __call__(self, *args, **kwargs):
            self.calls += 1
            return 'fallback-complete'

    cases = [dict(id=x) for x in ('normal', 'transient_once', 'persistent_transient', 'permanent', 'pre_stream_once', 'partial_stream', 'tool_timeout', 'circuit_recovery')]
    write_json(rec.output / 'fault-cases.json', cases)
    for repeat, case, variant in pairs(cases, repeats, ('protection_off', 'protection_on')):
        enabled = variant == 'protection_on'
        scenario = case['id']
        bus, fallback = TradeEventBus(), ExternalFallback()
        q = bus.subscribe('fault')
        context_token = ShoppingContext.set(ShoppingContextSnapshot('fault', 'synthetic', 'zh', 'CNY', f'{scenario}-{repeat}-{variant}'))
        started = time.perf_counter()
        error, chunks, calls, blocked, recovered, safe = None, [], 0, 0, None, True
        cooldown_observed_s = None
        try:
            if scenario.startswith('tool_') or scenario == 'circuit_recovery':
                registry = CircuitBreakerRegistry(failure_threshold=3, reset_seconds=.01)
                middleware = ToolResilienceMiddleware(registry, bus, {'external_tool': .01})
                tool = SimpleNamespace(name='external_tool')
                step = 0
                async def upstream(**kwargs):
                    nonlocal calls
                    calls += 1
                    if scenario == 'tool_timeout':
                        await asyncio.sleep(.03)
                    elif step < 5:
                        raise RuntimeError('503 service unavailable (injected)')
                    yield ToolChunk(content=[TextBlock(text='complete')], state=ToolResultState.SUCCESS)
                for step in range(1 if scenario == 'tool_timeout' else 6):
                    if step == 5:
                        cooldown_observed_s = await wait_monotonic(.012)
                    previous = calls
                    try:
                        output = [c async for c in middleware.on_tool_call(tool, {}, upstream)] if enabled else [c async for c in upstream()]
                        chunks.extend(str(c.state) for c in output)
                        recovered = bool(output and output[-1].state == ToolResultState.SUCCESS)
                    except Exception as exc:
                        error = safe_error(exc)
                        recovered = False
                    blocked += calls == previous
                success = bool(recovered)
                safe = (not success) if scenario == 'tool_timeout' and enabled else True
            else:
                model = ExternalFaultModel(scenario, credential=OpenAICredential(api_key='injected-not-a-secret', base_url='http://127.0.0.1:9'),
                                           model='injected-primary', throttle=GatewayThrottle(2, 0),
                                           max_transient_retries=2 if enabled else 0,
                                           retry_base_seconds=.002, fallback=fallback if enabled else None, bus=bus)
                try:
                    result = await model([])
                    if hasattr(result, '__aiter__'):
                        async for chunk in result:
                            chunks.append(chunk)
                    else:
                        chunks.append(result)
                except Exception as exc:
                    error = safe_error(exc)
                finally:
                    await model.client.close()
                calls = model.calls + fallback.calls
                success = not error and ('last' in chunks or 'fallback-complete' in chunks)
                if scenario == 'partial_stream':
                    safe = chunks == ['first'] and model.calls == 1 and fallback.calls == 0 and error is not None
                elif scenario == 'permanent':
                    safe = model.calls == 1 and fallback.calls == 0 and error is not None
            events = []
            while not q.empty():
                events.append(q.get_nowait().to_dict())
            rec.result('faults', scenario, variant, repeat, time.perf_counter()-started, success,
                       safe_failure=safe, upstream_calls=calls, blocked_calls=blocked, recovered=recovered,
                       error=error, chunks=chunks, events=events, mock=True,
                       mock_scope='Only external API boundary; planned fault classes, not natural traffic',
                       cooldown_observed_s=cooldown_observed_s,
                       retry_base_s=.002, tool_timeout_s=.01, circuit_reset_s=.01)
        finally:
            ShoppingContext.reset(context_token)
