"""Run frozen synthetic shopping needs against real project and web services."""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import shutil
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import yaml

from .common import ROOT, TRIAL, Recorder, digest, manifest, now, safe_error, write_json
from .live import container, database_snapshot, drain
from .observation import ApiCapture, install_tracing

DATASET = ROOT / 'eval/shopping_acceptance_v1.yaml'
PROTOCOL = ROOT / 'docs/shopping-acceptance-methodology.md'


def load_cases():
    data = yaml.safe_load(DATASET.read_text(encoding='utf-8'))
    cases = data['cases']
    assert len(cases) == 24 and len({c['id'] for c in cases}) == 24
    assert all(len(c['criteria']) == 3 and c['turns'] and c['mode'] in ('local', 'web') for c in cases)
    return cases


class WebCapture:
    def __init__(self, rec, max_searches):
        self.rec, self.maximum = rec, max_searches
        self.searches, self.offers = [], []
        self.attempts = 0

    def __enter__(self):
        from app.infrastructure.rag.web_source import TavilyWebSource
        from app.infrastructure.catalog import external_discovery
        original, offer_original = TavilyWebSource.search, external_discovery._merchant_offer
        capture = self

        async def search(source, query, max_results=5):
            if capture.attempts >= capture.maximum:
                raise RuntimeError('acceptance_web_search_budget_exhausted')
            capture.attempts += 1
            index = capture.attempts
            row = dict(TRIAL.get(), search_id=index, query=query, started_at=now())
            pages, error = [], None
            try:
                pages = await original(source, query, max_results=max_results)
                return pages
            except BaseException as exc:
                error = safe_error(exc)
                raise
            finally:
                artifact = capture.rec.artifact(f'web-search-{index:03}.json', dict(**row, pages=pages, error=error))
                row.update(finished_at=now(), page_count=len(pages), error=error, artifact=artifact,
                           sha256=digest(capture.rec.output / artifact))
                capture.searches.append(row)
                capture.rec.append('web-searches.jsonl', row)

        async def offer(url):
            observed = await offer_original(url)
            row = dict(TRIAL.get(), url=url, fetched_at=now(), offer=observed)
            capture.offers.append(row)
            capture.rec.append('merchant-offers.jsonl', row)
            return observed

        self.stack = ExitStack()
        self.stack.enter_context(patch.object(TavilyWebSource, 'search', search))
        self.stack.enter_context(patch.object(external_discovery, '_merchant_offer', offer))
        return self

    def __exit__(self, *args):
        self.stack.close()


async def run(args, rec, cases):
    from app.application.agents.orchestrator import SubmitIntentInput
    from app.infrastructure.settings import load_settings
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    settings = load_settings()
    write_json(rec.output/'environment.json', dict(model=settings.llm_model, tavily_configured=bool(settings.tavily_api_key),
        source='real configured provider; synthetic needs; isolated local stores', product_query_preauthorized=True,
        order_confirmation='explicit dialogue turns', model_max_calls=args.max_calls, web_max_searches=args.max_searches))
    if not settings.llm_api_key or not settings.tavily_api_key:
        raise RuntimeError('Live acceptance requires the configured LLM and Tavily credentials')
    tracer = install_tracing(rec)
    with ApiCapture(rec, args.max_calls) as model_capture, WebCapture(rec, args.max_searches) as web_capture:
        for case in cases:
            print('BEGIN ' + case['id'], flush=True)
            key = 'acceptance-' + case['id']
            token = TRIAL.set(dict(suite='shopping_acceptance', case_id=case['id'], variant=case['mode'], repeat=0))
            turns, error, residual, initial_stock = [], None, [], None
            started = time.perf_counter()
            before_model, before_web, before_offer = len(model_capture.calls), len(web_capture.searches), len(web_capture.offers)
            try:
                async with container(rec, key, consent=True, web=case['mode'] == 'web') as c:
                    q = c.bus.subscribe(key)
                    initial_stock = (await c.product_repo.find_by_id('P1008')).skus[0].stock
                    # Startup is recorded separately; task timing begins at user input.
                    started = time.perf_counter()
                    try:
                        async with asyncio.timeout(args.task_timeout):
                            for index, query in enumerate(case['turns']):
                                with tracer.start_as_current_span('acceptance.request'):
                                    carrier = {}
                                    TraceContextTextMapPropagator().inject(carrier)
                                    output = await c.orchestrator.handle_intent(SubmitIntentInput(key, key, 'zh-CN', 'CNY', query,
                                        request_id=f'{key}-{index}', traceparent=carrier['traceparent']))
                                snapshot = database_snapshot(c.settings.data_dir/'globex.db')
                                agent = await c.orchestrator._sessions.get_or_create(key)
                                events = drain(q)
                                turn = dict(query=query, text=output.final_text, transport_success=output.success,
                                    events=events, orders=snapshot['orders'], order_lines=snapshot['order_items'],
                                    preferences=snapshot['buyer_preferences'],
                                    lamp_stock=(await c.product_repo.find_by_id('P1008')).skus[0].stock,
                                    search_calls=[e['payload'] for e in events if e['type']=='tool.invoke' and e['payload'].get('tool')=='product_search_tool'],
                                    search_results=[e['payload'] for e in events if e['type']=='tool.result' and e['payload'].get('tool')=='product_search_tool'],
                                    state_artifact=rec.artifact(f'{case["id"]}-state-{index}.json', agent.state.model_dump(mode='json')))
                                turns.append(turn)
                                rec.append('turns.jsonl', dict(TRIAL.get(), turn=index, **turn))
                    except Exception as exc:
                        error = safe_error(exc)
                    finally:
                        residual = drain(q)
                        c.bus.unsubscribe(key, q)
                    catalog = [p.to_dict() if hasattr(p, 'to_dict') else __import__('dataclasses').asdict(p) for p in await c.product_repo.list_all()]
                    rec.artifact(case['id']+'-catalog.json', catalog)
            except Exception as exc:
                error = safe_error(exc)
            elapsed = time.perf_counter()-started
            calls = model_capture.calls[before_model:]
            web = web_capture.searches[before_web:]
            offers = web_capture.offers[before_offer:]
            checks = dict(all_turns_returned=len(turns)==len(case['turns']), no_execution_exception=error is None,
                          all_responses_nonempty=bool(turns) and all(bool(t['text'].strip()) for t in turns),
                          no_order_in_nontransaction_case=case['category']=='transaction' or all(not t['orders'] for t in turns))
            payload = dict(case=case, turns=turns, initial_lamp_stock=initial_stock, residual_events=residual,
                           web_searches=web, merchant_offers=offers, error=error, automatic_checks=checks)
            artifact = rec.artifact(case['id']+'.json', payload)
            available = lambda field: [r[field] for r in calls if r.get(field) is not None]
            row = dict(case_id=case['id'], category=case['category'], mode=case['mode'], elapsed_s=elapsed,
                       timestamp=now(), turns=len(turns), expected_turns=len(case['turns']), error=error,
                       model_calls=len(calls), input_tokens=sum(available('input_tokens')) if available('input_tokens') else None,
                       output_tokens=sum(available('output_tokens')) if available('output_tokens') else None,
                       usage_missing=sum(r.get('input_tokens') is None for r in calls), web_searches=len(web),
                       pages_observed=sum(r['page_count'] for r in web), automatic_checks=checks,
                       artifact=artifact, artifact_sha256=digest(rec.output/artifact), acceptance='awaiting_evidence_review')
            rec.append('observations.jsonl', row)
            print(json.dumps(dict(case_id=case['id'], turns=len(turns), web_searches=len(web), error=error)), flush=True)
            TRIAL.reset(token)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    p.add_argument('--max-calls', type=int, default=300)
    p.add_argument('--max-searches', type=int, default=40)
    p.add_argument('--task-timeout', type=float, default=180)
    p.add_argument('--case-ids', nargs='+', help='Explicit subset; separately reported supplemental run only')
    p.add_argument('--supplement-of', help='Original run directory; original observations remain authoritative')
    args = p.parse_args()
    cases = load_cases()
    if bool(args.case_ids) != bool(args.supplement_of):
        p.error('A case subset requires --supplement-of and vice versa')
    if args.case_ids:
        selected = set(args.case_ids)
        if len(selected) != len(args.case_ids) or not selected.issubset({c['id'] for c in cases}):
            p.error('Unknown or duplicate case IDs')
        cases = [c for c in cases if c['id'] in selected]
    root = Path(args.output)
    if root.exists() and any(root.iterdir()):
        p.error('Output must be new/empty; earlier observations are never overwritten')
    rec = Recorder(root)
    info = manifest(args)
    info.update(dataset_sha256=digest(DATASET), protocol_sha256=digest(PROTOCOL), planned_cases=len(cases),
                origin='synthetic', scoring='explicit evidence review, not response-based auto PASS')
    if args.supplement_of:
        original = Path(args.supplement_of)
        if not (original/'manifest.json').exists():
            p.error('Original run manifest not found')
        info.update(supplement_of=str(original.resolve()), supplemental=True,
                    original_observations_sha256=digest(original/'observations.jsonl'),
                    supplement_reason='Fresh measurement budget; do not replace or pool original observations')
    info['files'][str(PROTOCOL.relative_to(ROOT)).replace('\\','/')] = digest(PROTOCOL)
    write_json(root/'manifest.json', info)
    write_json(root/'frozen-cases.json', cases)
    for relative in info['files']:
        target = root/'source'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/relative, target)
    logging.basicConfig(filename=root/'runtime.log', level=logging.WARNING, encoding='utf-8')
    try:
        asyncio.run(run(args, rec, cases))
        info['status'] = 'finished'
    except BaseException as exc:
        info.update(status='runner_failed', error=safe_error(exc))
        raise
    finally:
        info['finished_at'] = now()
        info['source_unchanged'] = all(digest(ROOT/name)==sha for name,sha in info['files'].items() if name.startswith('app/'))
        write_json(root/'manifest.json', info)


if __name__ == '__main__':
    main()
