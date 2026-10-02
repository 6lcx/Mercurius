# -*- coding: utf-8 -*-
"""订单三个 UseCase：PlaceOrder / QueryOrder / CancelOrder

TradeAgent 的工具层只做参数搬运，业务规则（库存扣减、状态机、金额计算）全部收敛在这里与 Order 聚合内。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import asyncio
import hashlib
import json
import uuid
from copy import deepcopy

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.order.address import Address
from app.domain.order.order import Order
from app.domain.order.order_line import OrderLine
from app.domain.order.ports.order_repository import OrderRepository


@dataclass(frozen=True)
class OrderItemInput:
    product_id: str
    sku_id: str
    quantity: int


class PlaceOrderUseCase:
    def __init__(self, product_repo: ProductRepository, order_repo: OrderRepository) -> None:
        self._product_repo = product_repo
        self._order_repo = order_repo

    async def execute(self, buyer_id: str, items: list[OrderItemInput], shipping_address: Address, idempotency_key: str | None = None) -> dict:
        if not items:
            raise ValueError("PlaceOrder.items 不能为空")
        request_key = hashlib.sha256(f"{buyer_id}:{idempotency_key or uuid.uuid4().hex}".encode()).hexdigest()
        payload = {"buyer_id": buyer_id, "items": [asdict(i) for i in items], "address": asdict(shipping_address) if isinstance(shipping_address, Address) else None}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Memory fallback is intentionally process-local. SQLite uses its own
        # transaction and durable inventory/idempotency tables below.
        repo = self._order_repo
        if not hasattr(repo, "_placement_lock"):
            repo._placement_lock = asyncio.Lock()
            repo._placement_results = {}
        async with repo._placement_lock:
            if request_key in repo._placement_results:
                prior_fingerprint, result = repo._placement_results[request_key]
                if prior_fingerprint != fingerprint:
                    raise ValueError("幂等键已用于不同订单内容")
                return deepcopy(result)
            lines = []
            inventory = {}
            skus = {}
            for item in items:
                if isinstance(item.quantity, bool) or not isinstance(item.quantity, int) or item.quantity <= 0:
                    raise ValueError("订单数量必须为正整数")
                product = await self._product_repo.find_by_id(item.product_id)
                if product is None:
                    raise ValueError(f"商品不存在：{item.product_id}")
                if product.source_type != "local":
                    raise ValueError("外部商品请通过购买链接在商家页面下单，不能创建本地模拟订单")
                sku = product.find_sku(item.sku_id)
                if sku is None:
                    raise ValueError(f"Sku 不存在：{item.sku_id}")
                key = (item.product_id, item.sku_id)
                previous = inventory.get(key, (sku.stock, 0))
                inventory[key] = (previous[0], previous[1]+item.quantity)
                skus[key] = sku
                lines.append(OrderLine(product.product_id, sku.sku_id, f"{product.title}（{sku.spec}）", sku.price, item.quantity))
            if not isinstance(shipping_address, Address):
                raise ValueError("收货地址无效")
            order = Order.place(order_id=await repo.next_order_id(), buyer_id=buyer_id, shipping_address=shipping_address, lines=lines)
            if getattr(repo, "supports_atomic_orders", False):
                order = await repo.place_atomic(order, inventory, request_key, fingerprint)
                return order.snapshot()
            deducted = []
            try:
                for key, (_, quantity) in inventory.items():
                    skus[key].deduct_stock(quantity)
                    deducted.append((skus[key], quantity))
                await repo.save(order)
            except BaseException:
                for sku, quantity in deducted:
                    sku.restore_stock(quantity)
                raise
            result = order.snapshot()
            repo._placement_results[request_key] = (fingerprint, deepcopy(result))
            return result


class QueryOrderUseCase:
    def __init__(self, order_repo: OrderRepository) -> None:
        self._order_repo = order_repo

    async def execute(self, order_id: str) -> dict:
        order = await self._order_repo.find_by_id(order_id)
        if order is None:
            raise ValueError(f"订单不存在：{order_id}")
        return order.snapshot()


class CancelOrderUseCase:
    def __init__(self, product_repo: ProductRepository, order_repo: OrderRepository) -> None:
        self._product_repo = product_repo
        self._order_repo = order_repo

    async def execute(self, order_id: str, reason: str) -> dict:
        if getattr(self._order_repo, "supports_atomic_orders", False):
            return (await self._order_repo.cancel_atomic(order_id, reason)).snapshot()
        repo = self._order_repo
        if not hasattr(repo, "_placement_lock"):
            repo._placement_lock = asyncio.Lock()
            repo._placement_results = {}
        async with repo._placement_lock:
            order = await repo.find_by_id(order_id)
            if order is None:
                raise ValueError(f"订单不存在：{order_id}")
            from app.domain.order.order import OrderStatus
            if order.status == OrderStatus.CANCELLED:
                return order.snapshot()
            before = deepcopy(order.__dict__)
            restored = []
            try:
                order.cancel(reason)
                for line in order.lines:
                    product = await self._product_repo.find_by_id(line.product_id)
                    sku = product.find_sku(line.sku_id) if product is not None else None
                    if sku is not None:
                        sku.restore_stock(line.quantity)
                        restored.append((sku, line.quantity))
                await repo.save(order)
            except BaseException:
                for sku, quantity in restored:
                    sku.deduct_stock(quantity)
                order.__dict__.clear()
                order.__dict__.update(before)
                raise
            return order.snapshot()
