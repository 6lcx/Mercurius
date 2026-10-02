from __future__ import annotations
import asyncio
import json
import os
import sqlite3
import time
from contextlib import ExitStack, asynccontextmanager
from dataclasses import asdict, replace
from unittest.mock import patch

from .common import ROOT, TRIAL, pairs, safe_error, write_json
from .observation import ApiCapture, install_tracing, usage_fields


def base_settings():
    from app.infrastructure.settings import load_settings
    settings = load_settings()
    return replace(settings, embedding_provider='local', embedding_model='BAAI/bge-small-zh-v1.5', embedding_dim=512,
                   redis_url='', queue_enabled=False, semantic_cache_enabled=False, tavily_api_key='',
                   qdrant_url='', otlp_endpoint='', token_budget_total=0, reply_token_budget=0,
                   drift_detect_enabled=False, breaker_shared=False)


@asynccontextmanager
async def container(rec, key, variant='protection_on', consent=False, web=False):
    from app.composition import build_container
    from app.infrastructure.llm import create_chat_model as original
    from app.application.agents import main_agent, search_agent, trade_agent, orchestrator
    root = rec.output / 'state' / key
    root.mkdir(parents=True, exist_ok=False)
    settings = replace(base_settings(), data_dir=root, database_url=f'sqlite+aiosqlite:///{(root / "globex.db").as_posix()}')
    if web:
        from app.infrastructure.settings import load_settings
        settings = replace(settings, tavily_api_key=load_settings().tavily_api_key)
    if variant == 'protection_off':
        settings = replace(settings, harness_enabled=False, output_guard_enabled=False,
                           llm_max_retries=0, llm_fallback_model='')
    models = []
    def factory(*args, **kwargs):
        model = original(*args, **kwargs)
        for member in (model, model._fallback):
            if member is not None:
                member.max_retries = 0  # AgentScope retry loop
                member.client.max_retries = 0  # openai SDK retry loop
                member.parameters.max_tokens = 4096
                member.parameters.temperature = 0
                member.client.timeout = __import__('httpx').Timeout(90)
                models.append(member)
        return model
    with ExitStack() as stack:
        stack.enter_context(patch('app.composition.load_settings', lambda: settings))
        for module in (main_agent, search_agent, trade_agent):
            stack.enter_context(patch.object(module, 'create_chat_model', factory))
        if consent:
            from agentscope.permission import PermissionRule, PermissionBehavior
            for cls in (main_agent.MainAgentFactory, search_agent.SearchAgentFactory):
                build = cls.build
                def preauthorized(self, *args, _build=build, **kwargs):
                    agent = _build(self, *args, **kwargs)
                    agent.state.permission_context.allow_rules.setdefault('product_search_tool', []).append(
                        PermissionRule(tool_name='product_search_tool', rule_content=None,
                                       behavior=PermissionBehavior.ALLOW, source='projectSettings'))
                    return agent
                stack.enter_context(patch.object(cls, 'build', preauthorized))
        if variant == 'protection_off':
            for cls in (main_agent.MainAgentFactory, search_agent.SearchAgentFactory, trade_agent.TradeAgentFactory):
                stack.enter_context(patch.object(cls, '_resilience', lambda self: []))
            stack.enter_context(patch.object(orchestrator, '_MAX_TURN_RETRIES', 0))
        built = await build_container()
        try:
            started = time.perf_counter()
            await built.startup()
            rec.append('setup.jsonl', dict(operation='container_startup', key=key, elapsed_s=time.perf_counter()-started))
            yield built
        finally:
            await built.shutdown()
            for model in models:
                await model.client.close()


def database_snapshot(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return {name: [dict(row) for row in db.execute(f'SELECT * FROM {name}')]
                for name in ('orders', 'order_items', 'buyer_preferences', 'order_inventory')}


def drain(queue):
    rows = []
    while not queue.empty():
        event = queue.get_nowait().to_dict()
        if event['type'] != 'token.delta':
            rows.append(event)
    return rows


async def agent_suite(rec, capture, tracer, args):
    from .cases import agent_cases, evaluate
    from app.application.agents.orchestrator import SubmitIntentInput
    from app.domain.buyer.preference import BuyerPreference
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    cases = agent_cases()
    if args.limit_cases:
        cases = cases[:args.limit_cases]
    write_json(rec.output / 'agent-cases.json', cases)
    for repeat, case, variant in pairs(cases, args.repeats, ('protection_off', 'protection_on')):
        key = f'agent-{case["id"]}-{repeat}-{variant}'
        token = TRIAL.set(dict(suite='agent', case_id=case['id'], variant=variant, repeat=repeat,
                              stratum=getattr(args, 'stratum', 'normal')))
        turns, error, censored = [], None, False
        started = None
        async with container(rec, key, variant, args.consent_product_search) as c:
            sid, buyer = key, key[:56]
            if case.get('preference_fixture'):
                await c.orchestrator._preference_store.append(BuyerPreference(buyer, 'dislike', case['preference_fixture']))
            initial_stock = (await c.product_repo.find_by_id('P1008')).skus[0].stock
            q = c.bus.subscribe(sid)
            calls_before = len(capture.calls)
            started = time.perf_counter()
            try:
                async with asyncio.timeout(args.task_timeout):
                    for index, query in enumerate(case['queries']):
                        with tracer.start_as_current_span('benchmark.request') as span:
                            carrier = {}
                            TraceContextTextMapPropagator().inject(carrier)
                            output = await c.orchestrator.handle_intent(SubmitIntentInput(sid, buyer, 'zh-CN', 'CNY', query,
                                request_id=f'{key}-{index}', traceparent=carrier['traceparent']))
                        agent = await c.orchestrator._sessions.get_or_create(sid)
                        snapshot = database_snapshot(c.settings.data_dir / 'globex.db')
                        event_rows = drain(q)
                        permission = agent.state.permission_context.model_dump(mode='json')
                        rec.artifact(f'{key}-state-{index}.json', agent.state.model_dump(mode='json'))
                        turn = dict(query=query, text=output.final_text, transport_success=output.success,
                                    events=event_rows, orders=snapshot['orders'], order_lines=snapshot['order_items'],
                                    preferences=snapshot['buyer_preferences'],
                                    lamp_stock=(await c.product_repo.find_by_id('P1008')).skus[0].stock,
                                    initial_lamp_stock=initial_stock,
                                    catalog_ids=[p.product_id for p in await c.product_repo.list_all()],
                                    permission_pending=('waiting for your permission' in output.final_text.lower()),
                                    permission_context=permission)
                        turns.append(turn)
                        rec.append('turns.jsonl', dict(TRIAL.get(), turn=index, **turn))
            except Exception as exc:
                error, censored = safe_error(exc), isinstance(exc, TimeoutError)
            elapsed = time.perf_counter()-started
            event_rows = [e for turn in turns for e in turn['events']] + drain(q)
            c.bus.unsubscribe(sid, q)
            checks = evaluate(case, turns)
            checks['no_execution_error'] = error is None
            artifact = rec.artifact(key + '.json', dict(case=case, turns=turns, checks=checks, residual_events=event_rows))
            calls = capture.calls[calls_before:]
            rec.result('agent', case['id'], variant, repeat, elapsed, all(checks.values()),
                       checks=checks, error=error, censored=censored, artifact=artifact, mock=False,
                       model_calls=len(calls), input_tokens=sum(r.get('input_tokens') or 0 for r in calls),
                       output_tokens=sum(r.get('output_tokens') or 0 for r in calls),
                       business_events=len(event_rows),
                       request_id_coverage=sum(bool(e.get('request_id')) for e in event_rows)/len(event_rows) if event_rows else None,
                       traceparent_coverage=sum(bool(e.get('traceparent')) for e in event_rows)/len(event_rows) if event_rows else None,
                       consent_product_search=args.consent_product_search,
                       score_scope='minimum machine-verifiable development task contracts')
        TRIAL.reset(token)


async def cache_suite(rec, capture, args):
    from openai import AsyncOpenAI
    from app.application.prompts.loader import load_prompts
    from app.application.memory.preference_selector import render_preference_hint
    from app.domain.buyer.preference import BuyerPreference
    settings = base_settings()
    preferences = ['喜欢简洁设计', '不要塑料材质', '喜欢耐用商品', '喜欢轻量装备', '不要皮革材质', '喜欢深色',
                   '喜欢可充电产品', '不要红色', '喜欢小尺寸', '喜欢易清洁', '不要羊毛材质', '喜欢可维修产品']
    cases = [dict(id=f'buyer-{i:02}', preference=p, query=q) for i, (p, q) in enumerate(zip(preferences,
             ['露营灯怎么选？', '旅行用品怎么挑？', '耳机怎么选？', '背包怎么选？']*3))]
    if args.limit_cases:
        cases = cases[:args.limit_cases]
    write_json(rec.output / 'cache-cases.json', cases)
    system = load_prompts()['main_agent']['system_prompt']
    async with AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, max_retries=0, timeout=90) as client:
        for repeat, case, variant in pairs(cases, args.repeats, ('dynamic_system_prefix', 'stable_system_hint')):
            token = TRIAL.set(dict(suite='cache', case_id=case['id'], variant=variant, repeat=repeat))
            hint = render_preference_hint([BuyerPreference(case['id'], 'like' if case['preference'].startswith('喜欢') else 'dislike', case['preference'])])
            messages = [dict(role='system', content=hint + '\n' + system)] if variant == 'dynamic_system_prefix' else [dict(role='system', content=system), dict(role='user', content=hint)]
            messages.append(dict(role='user', content=case['query']))
            started, error, response = time.perf_counter(), None, None
            try:
                response = await client.chat.completions.create(model=settings.llm_model, messages=messages, temperature=0, max_tokens=512)
            except Exception as exc:
                error = safe_error(exc)
            fields = usage_fields(response.usage if response else None)
            rec.result('cache', case['id'], variant, repeat, time.perf_counter()-started,
                       response is not None and fields['cached_tokens'] is not None,
                       **fields, error=error, exposure='first_observed' if repeat == 0 else 'repeated',
                       final_text=response.choices[0].message.content if response and response.choices else None,
                       mock=False)
            TRIAL.reset(token)


async def parallel_suite(rec, capture, args):
    from app.application.tools.task_dispatch_tool import build_task_dispatch_tool
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    cases = [dict(id='camp-travel', tasks=['检索露营灯，返回真实工具结果。', '检索旅行三件套，返回真实工具结果。']),
               dict(id='audio-power', tasks=['检索降噪耳机，返回真实工具结果。', '检索充电器，返回真实工具结果。']),
               dict(id='hiking-towel', tasks=['检索登山杖，返回真实工具结果。', '检索速干毛巾，返回真实工具结果。'])]
    cases = getattr(args, 'parallel_cases', None) or cases
    if args.limit_cases:
        cases = cases[:args.limit_cases]
    write_json(rec.output / 'parallel-cases.json', cases)
    for repeat, case, variant in pairs(cases, args.repeats, ('serial', 'parallel')):
        key = f'parallel-{case["id"]}-{repeat}-{variant}'
        token = TRIAL.set(dict(suite='parallel', case_id=case['id'], variant=variant, repeat=repeat))
        async with container(rec, key, consent=args.consent_product_search) as c:
            factory = c.orchestrator._sessions._main_factory
            dispatch = build_task_dispatch_tool(factory._search_factory, factory._trade_factory, c.bus,
                preference_store=factory._preference_store, preference_selector=factory._preference_selector)
            ctx = ShoppingContext.set(ShoppingContextSnapshot(key, key, 'zh-CN', 'CNY', key))
            q = c.bus.subscribe(key)
            started, error, outputs, censored = time.perf_counter(), None, [], False
            async def dispatch_worker(index, task):
                worker_ctx = ShoppingContext.set(ShoppingContextSnapshot(key, key, 'zh-CN', 'CNY',
                    request_id=f'{key}-worker-{index}', raw_query=task))
                try:
                    return await dispatch('search_agent', task)
                finally:
                    ShoppingContext.reset(worker_ctx)
            try:
                async with asyncio.timeout(args.task_timeout):
                    if variant == 'parallel':
                        outputs = await asyncio.gather(*(dispatch_worker(i, task) for i, task in enumerate(case['tasks'])))
                    else:
                        for i, task in enumerate(case['tasks']):
                            outputs.append(await dispatch_worker(i, task))
            except Exception as exc:
                error = safe_error(exc)
                censored = isinstance(exc, TimeoutError)
            finally:
                elapsed = time.perf_counter()-started
                ShoppingContext.reset(ctx)
            events = drain(q)
            c.bus.unsubscribe(key, q)
            completions = [e for e in events if e['type'] == 'tool.result' and e['payload'].get('tool') == 'task_dispatch']
            searches = [e for e in events if e['type'] == 'tool.result' and e['payload'].get('tool') == 'product_search_tool' and isinstance(e['payload'].get('hits'), list) and not e['payload'].get('error')]
            texts = ['\n'.join(b.text for b in result.content if hasattr(b, 'text')) for result in outputs]
            from .worker_contract import search_contract
            output_checks = [search_contract(text, [e for e in events if e.get('request_id') == f'{key}-worker-{i}'])
                             for i, text in enumerate(texts)]
            expected_count = len(case['tasks'])
            success = error is None and len(completions) == expected_count and len(searches) >= expected_count and len(output_checks) == expected_count and all(output_checks)
            artifact = rec.artifact(key+'.json', dict(outputs=texts, events=events, per_worker_evidence_checks=output_checks))
            overlap = None
            if len(completions) == expected_count:
                from datetime import datetime
                spans = [(datetime.fromisoformat(e['payload']['started_at']).timestamp(), datetime.fromisoformat(e['payload']['finished_at']).timestamp()) for e in completions]
                overlap = max(0, min(s[1] for s in spans)-max(s[0] for s in spans))
            rec.result('parallel', case['id'], variant, repeat, elapsed, success,
                       error=error, censored=censored, consent_product_search=args.consent_product_search,
                       completed_dispatches=len(completions), evidenced_searches=len(searches), overlap_s=overlap,
                       artifact=artifact, mock=False)
        TRIAL.reset(token)


async def compression_fixtures():
    """Use returned facts, without assuming a title query ranks its source first."""
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from app.application.usecases.product_recommendation import ProductRecommendationService
    from app.domain.catalog.product_search_spec import ProductSearchSpec
    repo = InMemoryProductRepository()
    cases, fixtures = [], {}
    for i, (pid, turns) in enumerate([('P1008', 4), ('P1001', 4), ('P1004', 8), ('P1005', 8), ('P1010', 16), ('P1007', 16)]):
        product = await repo.find_by_id(pid)
        fixture = await ProductRecommendationService(repo).execute(ProductSearchSpec(product.title, top_k=1, ship_to='CN'))
        if not fixture['hits']:
            raise RuntimeError('Empty fixed compression fixture: ' + pid)
        case = dict(id=f'history-{i}', query=product.title, turns=turns,
                    query_source_product_id=pid, returned_product_id=fixture['hits'][0]['product_id'])
        cases.append(case)
        fixtures[case['id']] = fixture
    return cases, fixtures


async def compression_suite(rec, capture, args):
    from agentscope.agent import Agent
    from agentscope.message import Msg, UserMsg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
    from agentscope.tool import Toolkit
    from agentscope.state import AgentState
    from app.application.agents.context_policy import build_context_config
    from app.application.agents.critical_facts import CriticalFactsMiddleware
    from app.infrastructure.llm import create_chat_model
    cases, fixtures = await compression_fixtures()
    if args.limit_cases:
        cases = cases[:args.limit_cases]
    write_json(rec.output / 'compression-cases.json', cases)
    settings = base_settings()
    for repeat, case, variant in pairs(cases, args.compression_repeats, ('full_history', 'project_compressed')):
        token = TRIAL.set(dict(suite='compression', case_id=case['id'], variant=variant, repeat=repeat))
        result = fixtures[case['id']]
        target = result['hits'][0]
        facts = [target['product_id'], target['title'], str(target['price_major']), '731', '待确认']
        history = [UserMsg('buyer', '本轮预算731元，收货中国，当前操作待确认，不要下单。'),
                   Msg(name='assistant', role='assistant', content=[ToolCallBlock(id='fixture-result', name='product_search_tool', input=json.dumps({'normalized_query': case['query'], 'ship_to': 'CN', 'top_k': 1}))]),
                   Msg(name='tool', role='assistant', content=[ToolResultBlock(id='fixture-result', name='product_search_tool', state=ToolResultState.SUCCESS, output=json.dumps(result, ensure_ascii=False))])]
        for index in range(case['turns']):
            history += [UserMsg('buyer', f'第{index+1}轮讨论旅行注意事项，尚未确认购买。'),
                        Msg(name='assistant', role='assistant', content=[TextBlock(text='出行前核对目的地、行李限重和产品说明。不要把未知费用看成免费。'*18)])]
        model = create_chat_model(replace(settings, llm_fallback_model='', llm_max_retries=0), stream=True)
        model.max_retries = model.client.max_retries = 0
        model.parameters.temperature = 0
        model.parameters.max_tokens = 4096
        agent = Agent(name='compression_benchmark', system_prompt='你是购物助手。只使用已提供的历史事实。', model=model, toolkit=Toolkit(tools=[]),
                      context_config=build_context_config(settings.context_size, settings.tool_result_limit),
                      middlewares=[CriticalFactsMiddleware()], state=AgentState(context=history))
        before_input = await agent._prepare_model_input()
        before = await model.count_tokens(**before_input)
        # Small controlled context window guarantees actual compression; not deployment default.
        model.context_size = max(1024, int(before/.80))
        started, error, response = time.perf_counter(), None, None
        try:
            async with asyncio.timeout(args.task_timeout):
                if variant == 'project_compressed':
                    await agent.compress_context()
                after_input = await agent._prepare_model_input()
                after = await model.count_tokens(**after_input)
                probe = UserMsg('buyer', '只回答最初商品的完整product_id、标题、商品原价、预算和购买动作是否待确认。不要添加其他商品。')
                stream = await model([*after_input['messages'], probe])
                async for response in stream:
                    pass
        except Exception as exc:
            error = safe_error(exc)
            after_input, after = await agent._prepare_model_input(), None
        finally:
            await model.client.close()
        answer = '\n'.join(b.text for b in response.content if hasattr(b, 'text')) if response else ''
        retained = json.dumps([m.model_dump(mode='json') for m in after_input['messages']], ensure_ascii=False)
        exact = [f in retained for f in facts]
        answered = [f in answer for f in facts]
        actual = bool(agent.state.summary)
        success = error is None and all(answered) and (actual if variant == 'project_compressed' else True)
        artifact = rec.artifact(f'compression-{case["id"]}-{repeat}-{variant}.json', dict(facts=facts,
            before=[m.model_dump(mode='json') for m in before_input['messages']],
            after=[m.model_dump(mode='json') for m in after_input['messages']], answer=answer,
            exact_retention=exact, answer_retention=answered, state=agent.state.model_dump(mode='json')))
        rec.result('compression', case['id'], variant, repeat, time.perf_counter()-started, success,
                   before_estimated_tokens=before, after_estimated_tokens=after,
                   estimated_token_reduction=1-after/before if after is not None and before else None,
                   exact_fact_retention=sum(exact)/len(exact), answer_fact_retention=sum(answered)/len(answered),
                   actual_compression=actual, controlled_context_size=model.context_size, artifact=artifact, error=error,
                   mock=False, fixture_scope='synthetic history using actual product service output')
        TRIAL.reset(token)


async def run_live(rec, suite, args):
    settings = base_settings()
    from urllib.parse import urlsplit
    write_json(rec.output / 'safe-settings.json', dict(model=settings.llm_model, provider_host=urlsplit(settings.llm_base_url).hostname,
        fallback_model=settings.llm_fallback_model, max_concurrency=settings.llm_max_concurrency,
        min_interval_s=settings.llm_min_interval_seconds, project_retries=settings.llm_max_retries,
        embedding=settings.embedding_model, semantic_cache=False, tavily=False, queue=False,
        sdk_retries=0, temperature=0, agent_max_output_tokens=4096, consent_product_search=args.consent_product_search))
    tracer = install_tracing(rec)
    with ApiCapture(rec, args.max_calls) as capture:
        if suite == 'agent':
            await agent_suite(rec, capture, tracer, args)
        elif suite == 'cache':
            await cache_suite(rec, capture, args)
        elif suite == 'parallel':
            await parallel_suite(rec, capture, args)
        elif suite == 'compression':
            await compression_suite(rec, capture, args)
