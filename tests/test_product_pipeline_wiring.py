"""Runtime wiring checks: product discovery cannot fall back to article admission."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agentscope.message import ToolResultState

from app.application.agents.search_agent import SearchAgentFactory
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.usecases.product_recommendation import ProductRecommendationService
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.persistence.persistent_product_repository import PersistentProductRepository
from app.infrastructure.resilience import CircuitBreakerRegistry
from app.infrastructure.settings import load_settings
from app.infrastructure.throttle import GatewayThrottle


async def test_container_shopping_uses_persistent_product_service_not_article_gate(tmp_path, monkeypatch):
    import app.composition as composition

    monkeypatch.setenv("LLM_API_KEY", "test-no-network")
    settings = replace(load_settings(), data_dir=tmp_path, database_url="file", redis_url="",
                       tavily_api_key="test-no-network", otlp_endpoint="", reranker_base_url="")
    monkeypatch.setattr(composition, "load_settings", lambda: settings)
    monkeypatch.setattr(composition, "build_embedding_client", lambda _: SimpleNamespace(embed=AsyncMock()))
    monkeypatch.setattr(composition, "QdrantProductIndex", lambda _: SimpleNamespace(close=AsyncMock()))
    kb = SimpleNamespace(vector_store=SimpleNamespace(__aexit__=AsyncMock()))
    monkeypatch.setattr(composition, "build_category_knowledge_base", lambda _: kb)

    def forbidden(*args, **kwargs):
        raise AssertionError("Shopping must not instantiate article web admission")

    monkeypatch.setattr(composition, "build_web_knowledge_base", forbidden)
    monkeypatch.setattr(composition, "WebKnowledgeService", forbidden)
    container = await composition.build_container()
    try:
        assert isinstance(container.product_repo, PersistentProductRepository)
        assert isinstance(container.product_recommendation, ProductRecommendationService)
        assert container.web_knowledge_base is None
        factory = SearchAgentFactory(settings, container.product_recommendation, container.bus,
                                     kb, CircuitBreakerRegistry(), GatewayThrottle(1, 0))
        assert {tool.name for tool in factory.build_tools()} == {"product_search_tool", "category_insight_tool"}
    finally:
        await container.shutdown()


async def test_product_tool_forwards_generic_demand_refresh_and_purchase_link():
    query = "双人露营要一盏续航好、方便充电的灯，不知道买哪个"
    result = {"hits": [{"product_id": "EXT-test", "purchase_url": "https://merchant.example/products/lantern"}],
              "recall_strategy": "product_recommendation", "admitted_ids": ["EXT-test"],
              "discovery_status": "discovered"}
    service = SimpleNamespace(execute=AsyncMock(return_value=result))
    bus = TradeEventBus()
    queue = bus.subscribe("product-wiring")
    token = ShoppingContext.set(ShoppingContextSnapshot(shopping_session_id="product-wiring", buyer_id="buyer",
                                                       locale="zh-CN", currency="CNY"))
    try:
        response = await build_product_search_tool(service, bus)(query, top_k="3", refresh=True)
    finally:
        ShoppingContext.reset(token)
    assert response.state == ToolResultState.SUCCESS
    assert json.loads(response.content[0].text) == result
    spec = service.execute.call_args.args[0]
    assert spec.normalized_query == query
    assert spec.top_k == 3
    assert service.execute.call_args.kwargs == {"refresh": True}
    assert queue.qsize() == 2
    await queue.get()
    event = await queue.get()
    assert event.payload["hits"] == result["hits"]
