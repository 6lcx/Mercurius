"""Bounded live merchant-source acceptance; no LLM, purchases or seed offers."""
import argparse
import asyncio
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .common import Recorder, TRIAL, write_json, digest, safe_error
from .shopping_acceptance import WebCapture
from app.infrastructure.settings import load_settings
from app.infrastructure.catalog.external_discovery import ExternalProductDiscovery
from app.infrastructure.embedding.local_embedding import LocalEmbeddingClient
from app.infrastructure.persistence.persistent_product_repository import PersistentProductRepository
from app.application.usecases.product_recommendation import ProductRecommendationService
from app.domain.catalog.product_search_spec import ProductSearchSpec

CASES = [
    ('lantern', 'Fenix CL26R Pro camping lantern'),
    ('powerbank', 'Anker power bank'),
    ('suitcase', 'Samsonite suitcase'),
]


async def run(output):
    if output.exists():
        raise ValueError('Use a new output directory')
    settings = load_settings()
    if not settings.tavily_api_key:
        raise RuntimeError('Configured Tavily credential is missing')
    rec = Recorder(output)
    write_json(output/'protocol.json', {'cases': CASES, 'maximum_search_calls': 6,
        'scope': 'live merchant evidence; empty isolated catalog; same production discovery/service; no LLM',
        'checks': ['nonempty real product candidates', 'price and currency have merchant evidence',
                   'candidate sources persisted', 'unknown destination costs not converted to verified totals'],
        'not_measured': ['checkout price', 'authenticity', 'all platforms', 'same SKU global lowest price']})
    write_json(output/'source-hashes.json', {str(p): digest(p) for p in Path('app').rglob('*.py')})
    embedder = LocalEmbeddingClient()
    rows = []
    with WebCapture(rec, 6) as capture:
        for key, query in CASES:
            TRIAL.set({'suite': 'web-catalog', 'case_id': key})
            discovery = ExternalProductDiscovery(settings)
            repo = PersistentProductRepository(output/'state'/key, products=[])
            service = ProductRecommendationService(repo, discovery=discovery, embedder=embedder)
            error = None
            result = {}
            print('BEGIN', key, flush=True)
            try:
                async with asyncio.timeout(120):
                    result = await service.execute(ProductSearchSpec(query, top_k=3), refresh=True)
            except Exception as exc:
                error = safe_error(exc)
            products = await repo.list_all()
            checks = {
                'nonempty_candidates': bool(result.get('hits')),
                'priced_source_records': bool(products) and all(p.purchase_url and p.evidence.get('price_evidence')
                    and p.evidence.get('fetched_at') and p.skus[0].price.amount_in_minor_units > 0 for p in products),
            }
            # Destination-specific totals must remain unknown when shipping or
            # tax evidence is absent; avoid another network search for this check.
            destination = await ProductRecommendationService(repo, embedder=embedder).execute(
                ProductSearchSpec(query, ship_to='CN', price_max_major=10000, top_k=3))
            checks['unknown_cost_not_verified'] = all(
                h.get('landed_price', {}).get('landed_total_major') is not None
                for h in destination.get('hits', [])) and all(
                    all(p.evidence.get(k) is not None for k in ('shipping_major', 'tax_major'))
                    for p in products if p.product_id in {h['product_id'] for h in destination.get('hits', [])})
            rows.append({'case': key, 'query': query, 'error': error, 'checks': checks,
                         'domains': sorted({urlsplit(p.purchase_url).hostname for p in products}),
                         'admitted_products': len(products), 'recommended': len(result.get('hits', []))})
            rec.artifact(key+'.json', {'summary': rows[-1], 'result': result, 'destination_result': destination,
                                      'products': [asdict(p) for p in products], 'pages': discovery.last_pages})
            print('END', rows[-1], flush=True)
    write_json(output/'summary.json', {'cases': rows, 'search_calls': len(capture.searches),
        'search_errors': sum(bool(r['error']) for r in capture.searches),
        'domains': sorted({d for row in rows for d in row['domains']}), 'llm_calls': 0})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
