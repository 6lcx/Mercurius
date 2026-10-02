import pytest
from app.application.usecases.product_recommendation import ProductRecommendationService
from app.domain.catalog.product import Product
from app.domain.catalog.sku import Sku
from app.domain.catalog.money import Money
from app.domain.catalog.product_search_spec import ProductSearchSpec

class Repo:
    def __init__(self, products=()): self.products = {p.product_id: p for p in products}
    async def list_all(self): return list(self.products.values())
    async def upsert(self, product): self.products[product.product_id] = product

class Discovery:
    def __init__(self, products): self.products, self.calls = products, 0
    async def discover(self, spec): self.calls += 1; return self.products

def product(pid, external=False, price=100, title='露营灯 防水 充电', evidence=None):
    return Product(pid, title, 'Demo', '户外运动', 'CN', title,
        ships_to=['CN'], skus=[Sku(pid+'-sku', 'standard', Money.from_major_units(price, 'CNY'), 3)],
        source_type='external' if external else 'local',
        purchase_url='https://merchant.example/products/'+pid if external else '', evidence=evidence or {})

@pytest.mark.asyncio
async def test_sufficient_local_never_calls_web():
    discovery=Discovery([product('new',True)])
    service=ProductRecommendationService(Repo([product('old')]), discovery)
    result=await service.execute(ProductSearchSpec('露营灯', top_k=1))
    assert result['discovery_status']=='local_sufficient'
    assert discovery.calls==0
    assert result['hits'][0]['product_id']=='old'

@pytest.mark.asyncio
async def test_shortage_admits_additively_and_cache_stable(tmp_path):
    repo=Repo([product('old',title='耳机')]); discovery=Discovery([product('new',True)])
    cache=tmp_path/'cache.json'
    service=ProductRecommendationService(repo, discovery, cache_path=cache)
    spec=ProductSearchSpec('露营灯 防水 充电', top_k=3)
    first=await service.execute(spec, session_key='a')
    assert first['admitted_ids']==['new']
    assert set(repo.products)=={'old','new'}
    assert first['hits'][0]['score']==pytest.approx(first['hits'][0]['raw_score']*.8)
    repo.products['better']=product('better')
    second=await service.execute(spec, session_key='a')
    assert second['discovery_status']=='cache_hit'
    assert [h['product_id'] for h in second['hits']]==['new']
    assert discovery.calls==1
    reopened=ProductRecommendationService(repo, discovery, cache_path=cache)
    assert (await reopened.execute(spec,session_key='a'))['discovery_status']=='cache_hit'
    other=await reopened.execute(spec,session_key='b')
    assert other['discovery_status']!='cache_hit'

@pytest.mark.asyncio
async def test_unknown_fees_not_free_and_expensive_landed_filtered():
    cheap=product('cheap',True,20)
    expensive=product('expensive',True,20,evidence={'shipping_major':400,'tax_major':10,'ship_to':'CN'})
    affordable=product('affordable',True,20,evidence={'shipping_major':10,'tax_major':5,'ship_to':'CN'})
    service=ProductRecommendationService(Repo(),Discovery([cheap,expensive,affordable]))
    result=await service.execute(ProductSearchSpec('露营灯',ship_to='CN',price_max_major=100))
    assert result['admitted_ids']==['affordable']
    assert result['unverified_candidates'][0]['product_id']=='cheap'
    assert any(r['product_id']=='expensive' and r['reason']=='over_price_cap' for r in result['filtered_out'])
    assert result['hits'][0]['landed_price']['landed_total_major']==35

@pytest.mark.asyncio
async def test_cache_revalidates_price_and_stock_and_refresh():
    repo=Repo([product('old')]); discovery=Discovery([])
    service=ProductRecommendationService(repo,discovery)
    spec=ProductSearchSpec('露营灯',ship_to='CN',price_max_major=200,top_k=1)
    await service.execute(spec)
    repo.products['old'].skus[0].price=Money.from_major_units(500,'CNY')
    result=await service.execute(spec)
    assert not result['hits'] and result['discovery_status']!='cache_hit'
    repo.products['old'].skus[0].price=Money.from_major_units(100,'CNY')
    await service.execute(spec)
    assert (await service.execute(spec,refresh=True))['discovery_status']=='local_sufficient'
    repo.products['old'].skus[0].stock=0
    assert not (await service.execute(spec))['hits']

@pytest.mark.asyncio
async def test_duplicate_links_and_wrong_product_not_admitted():
    a,b=product('a',True),product('b',True)
    b.purchase_url=a.purchase_url
    repo=Repo()
    result=await ProductRecommendationService(repo,Discovery([a,b,product('wrong',True,title='耳机')])).execute(ProductSearchSpec('露营灯'))
    assert len(result['admitted_ids'])==1
    assert 'wrong' not in repo.products

@pytest.mark.asyncio
async def test_evidence_dimensions_change_but_unknown_not_rejected():
    unknown=product('u',True)
    strong=product('s',True,evidence={'source_verification':'official','warranty_months':24,'sales_count':5000,'brand_verified':True})
    result=await ProductRecommendationService(Repo(),Discovery([unknown,strong])).execute(ProductSearchSpec('露营灯'))
    assert set(result['admitted_ids'])=={'u','s'}
    assert result['hits'][0]['product_id']=='s'
    assert result['hits'][0]['score_dimensions']['source']>.5

@pytest.mark.asyncio
async def test_failed_search_returns_local_without_invented_candidates():
    class Broken:
        async def discover(self,spec): raise RuntimeError('offline')
    result=await ProductRecommendationService(Repo([product('old')]),Broken()).execute(ProductSearchSpec('露营灯',top_k=3))
    assert result['discovery_status']=='discovery_failed'
    assert result['hits'][0]['product_id']=='old'

@pytest.mark.asyncio
async def test_cross_language_requirement_recall():
    p=product('en',True,title='Rechargeable waterproof camping lantern')
    result=await ProductRecommendationService(Repo(),Discovery([p])).execute(ProductSearchSpec('想买露营灯，雨天用，充电方便'))
    assert result['admitted_ids']==['en']

@pytest.mark.asyncio
async def test_only_winners_persist_not_every_passing_page():
    repo=Repo([product('old')]); discovery=Discovery([product(str(i),True) for i in range(5)])
    result=await ProductRecommendationService(repo,discovery).execute(ProductSearchSpec('露营灯',top_k=2))
    assert len(result['admitted_ids'])==1
    assert len(repo.products)==2

@pytest.mark.asyncio
async def test_known_landed_price_ranks_without_budget_and_raw_text_not_echoed():
    evidence={'shipping_major':0,'tax_major':0,'ship_to':'CN','raw_evidence':'x'*4000}
    cheap=product('cheap',True,price=20,evidence=evidence)
    expensive=product('expensive',True,price=200,evidence=evidence)
    result=await ProductRecommendationService(Repo(),Discovery([expensive,cheap])).execute(ProductSearchSpec('露营灯',ship_to='CN'))
    assert result['hits'][0]['product_id']=='cheap'
    assert 'raw_evidence' not in result['hits'][0]['evidence']

@pytest.mark.asyncio
async def test_crossborder_landed_comparison_uses_common_currency():
    e={'shipping_major':10,'tax_major':5,'ship_to':'CN','cost_currency':'CNY'}
    p=product('usd',True,price=20,evidence=e)
    p.skus[0].price=Money.from_major_units(10,'USD')
    service=ProductRecommendationService(Repo(),Discovery([p]))
    result=await service.execute(ProductSearchSpec('露营灯',ship_to='CN',price_max_major=100))
    assert result['hits'][0]['landed_price']['landed_total_major']==86
    assert result['hits'][0]['landed_price']['currency']=='CNY'

@pytest.mark.asyncio
async def test_shared_charging_semantics_cannot_recommend_charger_as_lantern():
    class IdenticalEmbeddings:
        async def embed(self, text): return [1., 0.]
    charger=product('charger',title='VoltTrek USB-C 充电器')
    charger.description='适用于露营灯，充电方便，便携防水'
    repo=Repo([charger, product('lamp')]); discovery=Discovery([])
    result=await ProductRecommendationService(repo,discovery,embedder=IdenticalEmbeddings()).execute(
        ProductSearchSpec('露营灯 充电方便 便携',top_k=2))
    assert [h['product_id'] for h in result['hits']]==['lamp']
    assert discovery.calls==1
    assert any(r['product_id']=='charger' and r['reason']=='product_kind_mismatch' for r in result['filtered_out'])

@pytest.mark.asyncio
async def test_stale_external_refreshes_same_id_and_url_without_duplicate():
    from datetime import datetime, timezone
    old=product('old',True,price=100,evidence={'fetched_at':'2020-01-01T00:00:00Z'})
    new=product('new',True,price=80,evidence={'fetched_at':datetime.now(timezone.utc).isoformat()})
    new.purchase_url=old.purchase_url
    repo=Repo([old]); discovery=Discovery([new])
    result=await ProductRecommendationService(repo,discovery).execute(ProductSearchSpec('露营灯',top_k=1))
    assert result['updated_ids']==['old']
    assert result['admitted_ids']==[]
    assert list(repo.products)==['old']
    assert result['hits'][0]['price_major']==80
    assert discovery.calls==1

@pytest.mark.asyncio
async def test_persistence_failure_does_not_promote_candidate():
    class BrokenRepo(Repo):
        async def upsert(self, product): raise OSError('disk full')
    result=await ProductRecommendationService(BrokenRepo(),Discovery([product('new',True)])).execute(ProductSearchSpec('露营灯'))
    assert result['hits']==[]
    assert result['admitted_ids']==[]
    assert result['discovery_status']=='storage_failed'

@pytest.mark.asyncio
async def test_external_stock_change_invalidates_cached_recommendation():
    p=product('p',True)
    repo=Repo([p]); service=ProductRecommendationService(repo)
    spec=ProductSearchSpec('露营灯',top_k=1)
    assert (await service.execute(spec))['hits']
    p.evidence['availability']='out_of_stock'
    assert not (await service.execute(spec))['hits']

@pytest.mark.asyncio
async def test_transient_discovery_failure_does_not_cache_partial_results():
    class Recovering:
        calls=0
        async def discover(self,spec):
            self.calls+=1
            if self.calls==1: raise RuntimeError('temporarily offline')
            return [product('new',True)]
    discovery=Recovering(); service=ProductRecommendationService(Repo([product('local')]),discovery)
    spec=ProductSearchSpec('露营灯',top_k=2)
    assert (await service.execute(spec))['discovery_status']=='discovery_failed'
    result=await service.execute(spec)
    assert result['discovery_status']=='discovered'
    assert result['admitted_ids']==['new']
    assert discovery.calls==2

@pytest.mark.asyncio
async def test_equal_scores_retain_catalog_before_new_candidates():
    # Existing and new external offers have identical quality; local catalog
    # includes persisted external offers and must not churn on ID ordering.
    existing=product('z-existing',True)
    new=product('a-new',True)
    other=product('b-new',True)
    repo=Repo([existing])
    result=await ProductRecommendationService(repo,Discovery([new,other])).execute(ProductSearchSpec('露营灯',top_k=2))
    assert 'z-existing' in [h['product_id'] for h in result['hits']]
    assert len(result['admitted_ids'])==1
