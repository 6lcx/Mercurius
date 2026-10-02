"""Frozen paired English/Chinese intents to locate cross-language retrieval gaps."""
import asyncio
import json
from pathlib import Path
from .common import write_json, digest


ENGLISH = [
    'My neck hurts when I sleep on a plane. What could help?',
    'Light bothers me when I sleep. I want something to cover my eyes.',
    'I am worried my suitcase is too heavy and the airline will charge me extra.',
    'I want a cup for coffee with a simple understated design.',
    'My knees hurt when hiking and I need something to lean on.',
    'The ground under my tent is hard and cold when I sleep.',
    'My phone keeps running out of power when I am away from home.',
    'The wall sockets abroad are different and my plug will not fit.',
    'My clothes are creased and I need to make them presentable.',
    'I want one thing to protect me from both rain and strong sunshine.',
    'I need something to dry myself after a shower that dries quickly afterward.',
    'Hostel sheets feel dirty and I want to bring my own layer to sleep in.',
]


async def main():
    from app.application.usecases.product_recommendation import ProductRecommendationService
    from app.domain.catalog.product_search_spec import ProductSearchSpec
    from app.infrastructure.embedding.local_embedding import LocalEmbeddingClient
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    from scripts.eval.metrics import recall_at_k, mrr
    root=Path('output/metric-targets/cross-language-v1')
    root.mkdir(parents=True,exist_ok=False)
    source=[json.loads(s) for s in Path('eval/product_recall.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
    semantic=[r for r in source if r.get('kind')=='semantic']
    assert len(semantic)==len(ENGLISH)==12
    cases=[dict(id=f'cross-{i:02}',english=en,chinese=r['query'],relevant=r['relevant']) for i,(en,r) in enumerate(zip(ENGLISH,semantic))]
    write_json(root/'protocol.json',dict(cases=cases,top_k=8,
        scope='12 synthetic translated semantic development intents; paired labels inherited from existing Chinese dataset',
        variants=['english_rules','english_rules_embedding','chinese_reference'],
        chinese_reference='Diagnostic language-gap reference, NOT an implemented English query translation feature',
        no_paid_api=True))
    write_json(root/'source-hashes.json',{str(p):digest(p) for p in [Path(__file__),Path('eval/product_recall.jsonl'),Path('app/application/usecases/product_recommendation.py')]})
    embedder=LocalEmbeddingClient()
    repo=InMemoryProductRepository()
    # Memoize real vectors only to avoid repeatedly encoding the identical corpus.
    # Values are the actual BGE outputs, with no ground-truth labels used in scoring.
    corpus=[p.searchable_text() for p in await repo.list_all()]
    vectors=dict(zip(corpus,await embedder.embed_batch(corpus)))
    class Memo:
        async def embed(self,text):
            if text not in vectors:
                vectors[text]=await embedder.embed(text)
            return vectors[text]
        async def embed_batch(self,texts):
            return [await self.embed(t) for t in texts]
    rows=[]
    for case in cases:
        for variant in ('english_rules','english_rules_embedding','chinese_reference'):
            svc=ProductRecommendationService(repo,embedder=None if variant=='english_rules' else Memo())
            payload=await svc.execute(ProductSearchSpec(case['chinese'] if variant=='chinese_reference' else case['english'],top_k=8))
            ids=[h['product_id'] for h in payload['hits']]
            rows.append(dict(case_id=case['id'],variant=variant,retrieved=ids,relevant=case['relevant'],
                recall_at_8=recall_at_k(ids,case['relevant'],8),mrr=mrr(ids,case['relevant'])))
            write_json(root/'results.json',rows)
    summary={v:dict(recall_at_8=sum(r['recall_at_8'] for r in rows if r['variant']==v)/12,
                    mrr=sum(r['mrr'] for r in rows if r['variant']==v)/12)
             for v in ('english_rules','english_rules_embedding','chinese_reference')}
    write_json(root/'summary.json',summary)
    print(json.dumps(summary),flush=True)


if __name__=='__main__':
    asyncio.run(main())
