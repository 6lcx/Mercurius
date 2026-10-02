from copy import deepcopy

import pytest

from app.infrastructure.persistence.persistent_product_repository import PersistentProductRepository
from app.infrastructure.persistence.seed_products import build_seed_products
from app.infrastructure.persistence.in_memory_repositories import InMemoryOrderRepository
from app.application.usecases.order_usecases import PlaceOrderUseCase, OrderItemInput


async def test_additive_product_overlay_survives_restart_without_changing_seeds(tmp_path):
    repo = PersistentProductRepository(tmp_path)
    before = {p.product_id for p in await repo.list_all()}
    external = deepcopy(build_seed_products()[0])
    external.product_id = "EXT-fixture"
    external.source_type = "external"
    external.purchase_url = "https://example.com/products/fixture"
    external.evidence = {"warranty_evidence": "1 year warranty", "availability": "unknown"}
    await repo.upsert(external)
    await repo.upsert(external)
    reopened = PersistentProductRepository(tmp_path)
    assert {p.product_id for p in await reopened.list_all()} == before | {external.product_id}
    restored = await reopened.find_by_id(external.product_id)
    assert restored.purchase_url == external.purchase_url
    assert restored.skus == external.skus
    assert restored.evidence == external.evidence
    with pytest.raises(ValueError, match="seed"):
        await reopened.upsert(build_seed_products()[0])


async def test_external_purchase_never_creates_a_local_demo_order(tmp_path):
    repo = PersistentProductRepository(tmp_path)
    external = deepcopy(build_seed_products()[0])
    external.product_id = "EXT-fixture"
    external.source_type = "external"
    await repo.upsert(external)
    orders = InMemoryOrderRepository()
    with pytest.raises(ValueError, match="购买链接"):
        await PlaceOrderUseCase(repo, orders).execute("buyer", [OrderItemInput(external.product_id, external.skus[0].sku_id, 1)], None)
    assert orders._orders == {}
