"""Web ingestion source tests; all network responses are deterministic HTTP fixtures."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.infrastructure.rag import web_source


def source_with_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(web_source.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    return web_source.TavilyWebSource(SimpleNamespace(tavily_api_key="test-only-key"))


async def test_raw_body_is_preserved_beyond_search_snippet(monkeypatch):
    body = "Full article body. " * 100
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.path == "/search"
        payload = json.loads(request.content)
        assert payload["include_raw_content"] == "text"
        return httpx.Response(200, json={"results": [{"url": "https://example.com/article", "title": "Article", "content": "short snippet", "raw_content": body}]})
    rows = await source_with_transport(monkeypatch, handle).search("question")
    assert rows == [{"url": "https://example.com/article", "title": "Article", "content": body.strip()}]
    assert len(requests) == 1


async def test_missing_raw_body_uses_extract_and_never_indexes_snippet(monkeypatch):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": "https://example.com/a", "title": "A", "content": "snippet only"}, {"url": "https://example.com/b", "title": "B", "content": "snippet only"}]})
        assert request.url.path == "/extract"
        assert json.loads(request.content)["urls"] == ["https://example.com/a", "https://example.com/b"]
        return httpx.Response(200, json={"results": [{"url": "https://example.com/a", "raw_content": "extracted complete article"}], "failed_results": [{"url": "https://example.com/b"}]})
    rows = await source_with_transport(monkeypatch, handle).search("question")
    assert calls == ["/search", "/extract"]
    assert rows == [{"url": "https://example.com/a", "title": "A", "content": "extracted complete article"}]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/a", "http://localhost/a", "http://169.254.169.254/", "http://10.0.0.1/", "http://[::1]/", "https://user:password@example.com/a", "not a url"])
async def test_unsafe_result_urls_never_become_ingestion_candidates(monkeypatch, url):
    def handle(request):
        assert request.url.path == "/search", "unsafe URLs must not be passed to extraction"
        return httpx.Response(200, json={"results": [{"url": url, "title": "bad", "raw_content": "body"}]})
    assert await source_with_transport(monkeypatch, handle).search("question") == []


async def test_timeout_is_propagated_to_service_for_fail_closed_handling(monkeypatch):
    def handle(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)
    with pytest.raises(httpx.TimeoutException):
        await source_with_transport(monkeypatch, handle).search("question")


async def test_result_count_and_body_size_are_bounded(monkeypatch):
    def handle(request):
        assert json.loads(request.content)["max_results"] <= 5
        return httpx.Response(200, json={"results": [{"url": "https://example.com/a", "title": "A", "raw_content": "x" * 40000}]})
    rows = await source_with_transport(monkeypatch, handle).search("question", max_results=99)
    assert 500 < len(rows[0]["content"]) <= 20000


async def test_purchase_query_preserves_constraints_and_requests_store_pages(monkeypatch):
    query = "购买 Fenix CL26R PRO 商品链接，预算100美元"
    def handle(request):
        payload = json.loads(request.content)
        assert '"CL26R PRO"' in payload["query"]
        assert "product price buy" in payload["query"]
        assert "-inurl:blog" in payload["query"]
        return httpx.Response(200, json={"results": []})
    assert await source_with_transport(monkeypatch, handle).search(query) == []

