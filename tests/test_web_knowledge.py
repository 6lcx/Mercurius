"""Admission policy tests: discount before selection; never store rejected passages."""
import json
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.application.usecases.web_knowledge import WebKnowledgeService


def hit(score, name="local", *, metadata=None):
    return SimpleNamespace(score=score, document_id=name, chunk=SimpleNamespace(content={"text": name}, metadata=metadata or {"source": name + ".md"}))


def service(local_scores=(.9, .7, .5), web_scores=(), *, weight=.8):
    local = SimpleNamespace(search=AsyncMock(return_value=[hit(score, f"local-{i}") for i, score in enumerate(local_scores)]))
    web = SimpleNamespace(search=AsyncMock(return_value=[hit(score, f"web-{i}", metadata={"url": f"https://example.com/{i}", "source_type": "web"}) for i, score in enumerate(web_scores)]), insert_document=AsyncMock())
    source = SimpleNamespace(search=AsyncMock(return_value=[]))
    return WebKnowledgeService(local, web, source, weight=weight)


async def test_discount_changes_rank_and_keeps_source_markers_json_safe():
    high = await service(web_scores=(.95,), weight=.8).search("question")
    low = await service(web_scores=(.95,), weight=.5).search("question")
    assert [x["source_type"] for x in high["insights"]] == ["local", "web", "local"]
    assert all(x["source_type"] == "local" for x in low["insights"])
    web = high["insights"][1]
    assert web["score"] == pytest.approx(.76)
    assert web["raw_score"] == .95
    assert web["source"] == "https://example.com/0"
    json.dumps(high, allow_nan=False)


async def test_equal_to_local_tail_is_rejected_strictly():
    result = await service(local_scores=(.9, .7, .4), web_scores=(.5,)).search("question")
    assert all(x["source_type"] == "local" for x in result["insights"])


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_local_baseline_fails_closed(score):
    instance = service(local_scores=(score,))
    result = await instance.search("question", discover=True)
    assert result["insights"] == []
    instance.source.search.assert_not_awaited()
    instance.web_kb.insert_document.assert_not_awaited()


@pytest.mark.parametrize("broken", [False, True])
async def test_missing_or_failed_local_search_does_not_fetch_or_admit(broken):
    instance = service(local_scores=())
    if broken:
        instance.local_kb.search.side_effect = RuntimeError("local unavailable")
    result = await instance.search("question", discover=True)
    assert result["insights"] == []
    instance.source.search.assert_not_awaited()
    instance.web_kb.search.assert_not_awaited()
    instance.web_kb.insert_document.assert_not_awaited()


async def test_stored_web_must_pass_current_query_gate_again():
    instance = service(web_scores=(.95,))
    first = await instance.search("first")
    assert any(x["source_type"] == "web" for x in first["insights"])
    instance.local_kb.search.return_value = [hit(.99), hit(.95), hit(.8)]
    second = await instance.search("second")
    assert all(x["source_type"] == "local" for x in second["insights"])


async def test_negative_scores_cannot_pass_zero_floor():
    result = await service(local_scores=(-.1,), web_scores=(-.01,)).search("question", top_k=1)
    assert result["gate"]["threshold"] == 0
    assert result["insights"][0]["source_type"] == "local"


@pytest.mark.parametrize("weight", [0, 1, -.1, 1.1, float("nan"), float("inf")])
def test_weight_must_be_finite_trust_discount(weight):
    with pytest.raises(ValueError):
        service(weight=weight)


@pytest.mark.parametrize("top_k", [0, 11, -1, True, 2.5])
async def test_invalid_limits_rejected(top_k):
    with pytest.raises(ValueError):
        await service().search("question", top_k=top_k)

class DeterministicEmbedding:
    dimensions = 2
    supports_multimodal = False

    async def __call__(self, inputs):
        vectors = []
        for item in inputs:
            text = item if isinstance(item, str) else item.text
            # Each complete fixture passage carries a score marker. Query points on x-axis.
            if text == "question":
                score = 1.0
            elif "STRONG" in text:
                score = .99
            elif "MEDIUM" in text:
                score = .9
            elif "WEAK" in text:
                score = .1
            elif text.startswith("local:"):
                score = float(text.split(":")[1])
            else:
                score = .85
            vectors.append([score, math.sqrt(1 - score * score)])
        return SimpleNamespace(embeddings=vectors)


def discover_service(rows, local_scores=(.9, .7, .5)):
    instance = service(local_scores=local_scores)
    instance.local_kb.embedding_model = DeterministicEmbedding()
    instance.web_kb.list_documents = AsyncMock(return_value=[])
    instance.source.search.return_value = rows
    return instance


def page(content, suffix="a"):
    return {"url": f"https://example.com/{suffix}", "title": suffix, "content": content}


async def test_product_discovery_rejects_article_wrong_model_and_deduplicates_url():
    rows = [
        {"url": "https://shop.example/products/cl26r-pro", "title": "Fenix CL26R PRO", "content": "Fenix CL26R PRO\n$79.95\nAdd to cart\nSTRONG USB-C 5000mAh"},
        {"url": "https://shop.example/products/cl28r", "title": "Fenix CL28R", "content": "Fenix CL28R\n$99.95\nAdd to cart\nSTRONG"},
        {"url": "https://shop.example/blog/cl26r-pro", "title": "CL26R PRO review", "content": "CL26R PRO\n$79.95\nAdd to cart\nSTRONG"},
    ]
    instance = discover_service(rows)
    result = await instance.search("购买 Fenix CL26R PRO 商品链接", discover=True)
    web = [row for row in result["insights"] if row["source_type"] == "web"]
    assert [row["url"] for row in web] == [rows[0]["url"]]
    assert instance.web_kb.insert_document.await_count == 1


async def test_old_stored_article_cannot_bypass_product_policy():
    instance = service(web_scores=(.99,))
    result = await instance.search("购买 CL26R PRO 商品链接")
    assert all(row["source_type"] == "local" for row in result["insights"])


def test_product_chunks_from_same_url_only_take_one_slot():
    rows = [
        {"source_type": "web", "url": "https://shop.example/products/a", "score": .7},
        {"source_type": "web", "url": "https://shop.example/products/a", "score": .6},
        {"source_type": "local", "score": .5},
    ]
    assert WebKnowledgeService._unique_products(rows, "购买商品链接") == [rows[0], rows[2]]


async def test_only_final_top_k_displacing_chunks_are_inserted():
    instance = discover_service([page("STRONG", "a"), page("MEDIUM", "b"), page("ordinary", "c"), page("WEAK", "d")])
    result = await instance.search("question", discover=True)
    assert result["admitted_count"] == 2
    assert len(result["insights"]) == 3
    assert instance.web_kb.insert_document.await_count == 2
    persisted = [call.kwargs["chunks"][0].content.text for call in instance.web_kb.insert_document.await_args_list]
    assert set(persisted) == {"STRONG", "MEDIUM"}
    assert all(x["score"] > .5 for x in result["insights"] if x["source_type"] == "web")


async def test_rejected_page_is_never_written():
    instance = discover_service([page("WEAK")])
    result = await instance.search("question", discover=True)
    assert result["admitted_count"] == 0
    instance.web_kb.insert_document.assert_not_awaited()


async def test_long_page_is_gated_per_chunk_not_as_a_whole():
    body = ("STRONG relevant passage. " * 130) + "\n\n" + ("WEAK irrelevant passage. " * 300)
    instance = discover_service([page(body)])
    result = await instance.search("question", discover=True)
    assert result["admitted_count"] > 0
    stored = [call.kwargs["chunks"][0].content.text for call in instance.web_kb.insert_document.await_args_list]
    assert all("STRONG" in text for text in stored)
    assert all(len(text) < len(body) for text in stored)


async def test_failed_storage_cannot_recommend_unpersisted_content():
    instance = discover_service([page("STRONG")])
    instance.web_kb.insert_document.side_effect = RuntimeError("disk failure")
    result = await instance.search("question", discover=True)
    assert result["admitted_count"] == 0
    assert all(x["source_type"] == "local" for x in result["insights"])


async def test_provider_failure_returns_local_results():
    instance = discover_service([])
    instance.source.search.side_effect = httpx.ReadTimeout("timeout")
    result = await instance.search("question", discover=True)
    assert len(result["insights"]) == 3
    assert all(x["source_type"] == "local" for x in result["insights"])
    instance.web_kb.insert_document.assert_not_awaited()


async def test_real_agentscope_qdrant_persists_deduplicates_and_recalls(tmp_path):
    from agentscope.message import TextBlock
    from agentscope.rag import Chunk, KnowledgeBase, QdrantStore

    store = QdrantStore(path=str(tmp_path / "qdrant"))
    embedding = DeterministicEmbedding()
    local = KnowledgeBase(name="local", description="fixture", embedding_model=embedding, vector_store=store, collection="local")
    web = KnowledgeBase(name="web", description="fixture", embedding_model=embedding, vector_store=store, collection="web")
    def chunk(text):
        return Chunk(content=TextBlock(text=text), source="fixture.md", chunk_index=0, total_chunks=1, metadata={"source": "fixture.md"})
    try:
        for i, score in enumerate((.9, .7, .5)):
            await local.insert_document([chunk(f"local:{score}")], document_id=f"local-{i}")
        source = SimpleNamespace(search=AsyncMock(return_value=[page("STRONG"), page("WEAK", "bad")]))
        instance = WebKnowledgeService(local, web, source)
        first = await instance.search("question", discover=True)
        assert first["admitted_count"] == 1
        assert len(await web.list_documents()) == 1
        again = await instance.search("question", discover=True)
        assert again["admitted_count"] == 0
        assert len(await web.list_documents()) == 1
    finally:
        await store.__aexit__(None, None, None)

    reopened_store = QdrantStore(path=str(tmp_path / "qdrant"))
    try:
        reopened_local = KnowledgeBase(name="local", description="fixture", embedding_model=embedding, vector_store=reopened_store, collection="local")
        reopened_web = KnowledgeBase(name="web", description="fixture", embedding_model=embedding, vector_store=reopened_store, collection="web")
        recalled = await WebKnowledgeService(reopened_local, reopened_web, None).search("question")
        rows = [x for x in recalled["insights"] if x["source_type"] == "web"]
        assert len(rows) == 1
        assert rows[0]["content"] == "STRONG"
        assert rows[0]["url"] == "https://example.com/a"
        assert rows[0]["score"] == pytest.approx(.99 * .8)
        json.dumps(recalled, allow_nan=False)
    finally:
        await reopened_store.__aexit__(None, None, None)


async def test_stable_qdrant_points_make_cross_handle_retries_idempotent(tmp_path):
    import asyncio
    from agentscope.message import TextBlock
    from agentscope.rag import Chunk, KnowledgeBase
    from app.infrastructure.rag.web_vector_store import WebQdrantStore

    store = WebQdrantStore(path=str(tmp_path / "stable"))
    kwargs = dict(name="web", description="fixture", embedding_model=DeterministicEmbedding(), vector_store=store, collection="web")
    first, second = KnowledgeBase(**kwargs), KnowledgeBase(**kwargs)
    c0 = Chunk(content=TextBlock(text="STRONG"), source="url", chunk_index=0, total_chunks=2)
    c1 = Chunk(content=TextBlock(text="MEDIUM"), source="url", chunk_index=1, total_chunks=2)
    try:
        await first.ensure_collection()
        await second.ensure_collection()
        await asyncio.gather(first.insert_document([c0], document_id="same"), second.insert_document([c0], document_id="same"))
        assert (await store.get_client().count(collection_name="web", exact=True)).count == 1
        await second.insert_document([c1], document_id="same")
        await second.insert_document([c0], document_id="different")
        assert (await store.get_client().count(collection_name="web", exact=True)).count == 3
        assert len(await first.search(["question"], top_k=5)) == 3
    finally:
        await store.__aexit__(None, None, None)


async def test_web_tool_without_service_fails_without_http(monkeypatch):
    from agentscope.message import ToolResultState
    from app.application.tools.web_search_tool import build_web_search_tool
    from app.infrastructure.eventbus import TradeEventBus

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("unconfigured tool must not fetch"))
    tool = build_web_search_tool(SimpleNamespace(tavily_api_key="test"), TradeEventBus())
    assert (await tool("question")).state == ToolResultState.ERROR


@pytest.mark.parametrize("kind", ["web", "category"])
async def test_local_unavailable_is_tool_error(kind):
    from agentscope.message import ToolResultState
    from agentscope.tool import FunctionTool
    from app.application.tools.web_search_tool import build_web_search_tool
    from app.application.tools.category_insight_tool import build_category_insight_tool
    from app.infrastructure.eventbus import TradeEventBus

    mock = SimpleNamespace(search=AsyncMock(return_value={"insights": [], "gate": {"status": "local_unavailable", "threshold": None}, "admitted_count": 0}))
    builder = build_web_search_tool if kind == "web" else build_category_insight_tool
    fn = builder(SimpleNamespace(tavily_api_key="test"), TradeEventBus(), service=mock)
    wrapped = FunctionTool(fn, is_read_only=(kind == "category"))
    assert wrapped.input_schema["properties"]["query" if kind == "web" else "question"]["type"] == "string"
    result = await wrapped(**{"query" if kind == "web" else "question": "question"})
    if hasattr(result, "__aiter__"):
        result = [part async for part in result][-1]
    assert result.state == ToolResultState.ERROR


async def test_factory_tools_share_service_and_correct_mutation_flags():
    from app.application.agents.search_agent import SearchAgentFactory
    from app.infrastructure.eventbus import TradeEventBus

    mock = SimpleNamespace(search=AsyncMock(return_value={"insights": [], "gate": {"status": "local_empty"}, "admitted_count": 0}))
    factory = SearchAgentFactory(SimpleNamespace(tavily_api_key="test"), None, TradeEventBus(), None, None, None, web_knowledge_service=mock)
    # MainAgent and SearchAgent both consume build_tools; independent tool objects retain same service.
    for batch in range(2):
        tools = {tool.name: tool for tool in factory.build_tools()}
        assert tools["category_insight_tool"].is_read_only is True
        assert tools["web_search_tool"].is_read_only is False
        assert tools["web_search_tool"].input_schema["properties"]["max_results"]["type"] == "integer"
        for name, arg in [("category_insight_tool", "question"), ("web_search_tool", "query")]:
            tool = tools[name]
            # Invoke the underlying wrapped callable, bypassing unrelated resilience dependencies.
            fn = tool._func
            await fn(**{arg: "question"})
    assert mock.search.await_count == 4
    assert [call.kwargs["discover"] for call in mock.search.await_args_list] == [False, True, False, True]

@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_stored_web_score_never_recommended(score):
    instance = service(web_scores=(score,))
    result = await instance.search("question")
    assert all(row["source_type"] == "local" for row in result["insights"])
    json.dumps(result, allow_nan=False)


async def test_nonfinite_new_embedding_never_admitted():
    instance = discover_service([page("STRONG")])
    instance.local_kb.embedding_model = AsyncMock(return_value=SimpleNamespace(embeddings=[[float("nan"), 1.0]]))
    result = await instance.search("question", discover=True)
    assert result["admitted_count"] == 0
    instance.web_kb.insert_document.assert_not_awaited()
    assert all(row["source_type"] == "local" for row in result["insights"])
