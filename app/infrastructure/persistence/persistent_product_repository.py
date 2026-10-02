"""Demo catalog: immutable seed baseline plus an atomic additive JSON overlay.

Single-process demo storage; it is not a multi-worker database.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from app.domain.catalog.product import Product, ProductHighlight
from app.domain.catalog.sku import Sku
from app.domain.catalog.money import Money
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository


class PersistentProductRepository(InMemoryProductRepository):
    def __init__(self, data_dir: Path, products=None):
        super().__init__(products)
        self._inventory_reader = None
        self._seed_ids = set(self._products)
        self.path = Path(data_dir) / "external_products.json"
        self._lock = asyncio.Lock()
        self._extra = {}
        if self.path.exists():
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            for row in rows:
                product = _decode(row)
                if product.product_id in self._seed_ids:
                    raise ValueError("External catalog cannot overwrite a seed product")
                self._extra[product.product_id] = product
            self._products.update(self._extra)

    def set_inventory_reader(self, reader) -> None:
        self._inventory_reader = reader

    async def _with_inventory(self, products: list[Product]) -> list[Product]:
        if self._inventory_reader is None:
            return products
        from copy import deepcopy
        stocks = await self._inventory_reader([product.product_id for product in products])
        result = deepcopy(products)
        for product in result:
            for sku in product.skus:
                sku.stock = stocks.get((product.product_id, sku.sku_id), sku.stock)
        return result

    async def find_by_id(self, product_id: str):
        product = await super().find_by_id(product_id)
        return (await self._with_inventory([product]))[0] if product is not None else None

    async def find_by_ids(self, product_ids: list[str]) -> list[Product]:
        return await self._with_inventory(await super().find_by_ids(product_ids))

    async def list_all(self) -> list[Product]:
        return await self._with_inventory(await super().list_all())

    async def upsert(self, product: Product) -> None:
        if product.product_id in self._seed_ids:
            raise ValueError("External catalog cannot overwrite a seed product")
        async with self._lock:
            updated = {**self._extra, product.product_id: product}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps([asdict(p) for p in updated.values()], ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(self.path)
            self._extra = updated
            self._products[product.product_id] = product


def _decode(row: dict) -> Product:
    values = dict(row)
    values["highlights"] = [ProductHighlight(**h) for h in values.get("highlights", [])]
    values["skus"] = [Sku(sku_id=s["sku_id"], spec=s["spec"], stock=s["stock"],
                          price=Money.of(s["price"]["amount_in_minor_units"], s["price"]["currency"]))
                      for s in values["skus"]]
    return Product(**values)
