"""Fetch full web text through Tavily; never request arbitrary result URLs locally."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.infrastructure.settings import Settings
from app.application.usecases.web_product_policy import is_product_query, requested_models


def is_public_url(url: str) -> bool:
    """Reject local destinations, credentials and non-HTTP URLs before provider extraction."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("https", "http") or not host or parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 80, 443):
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host and not host.replace(".", "").isdigit()
    except (ValueError, TypeError):
        return False


class TavilyWebSource:
    MAX_RESULTS = 5
    MAX_CONTENT_CHARS = 20_000

    def __init__(self, settings: Settings, *, rewrite_product_queries: bool = True) -> None:
        self._api_key = settings.tavily_api_key
        self._rewrite_product_queries = rewrite_product_queries

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        if not self._api_key:
            return []
        count = max(1, min(int(max_results), self.MAX_RESULTS))
        if self._rewrite_product_queries and is_product_query(query):
            # Use precise model keywords for discovery. The admission service
            # still evaluates the original question, including its constraints.
            models = " OR ".join(f'"{model}"' for model in requested_models(query))
            query = f'{models or query} product price buy -inurl:blog -inurl:review'
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": self._api_key, "query": query, "max_results": count,
                      "search_depth": "basic", "include_raw_content": "text"},
            )
            response.raise_for_status()
            rows = response.json().get("results", [])
            candidates, seen = [], set()
            for row in rows[:count]:
                url = row.get("url", "")
                if not is_public_url(url):
                    continue
                parsed = urlsplit(url)
                url = urlunsplit(parsed._replace(fragment=""))
                if url in seen:
                    continue
                seen.add(url)
                candidates.append({"url": url, "title": str(row.get("title") or "")[:500],
                                   "content": row.get("raw_content")})
            missing = [row["url"] for row in candidates if not isinstance(row["content"], str)
                       or not row["content"].strip()]
            if missing:
                try:
                    response = await client.post(
                        "https://api.tavily.com/extract",
                        json={"api_key": self._api_key, "urls": missing,
                              "extract_depth": "basic", "format": "text"},
                    )
                    response.raise_for_status()
                    extracted = {row.get("url"): row.get("raw_content")
                                 for row in response.json().get("results", [])}
                    for row in candidates:
                        if row["url"] in missing:
                            row["content"] = extracted.get(row["url"])
                except (httpx.HTTPError, ValueError):
                    # Partial provider failure must not discard full text already fetched.
                    if not any(isinstance(row["content"], str) and row["content"].strip()
                               for row in candidates):
                        raise
        return [{**row, "content": row["content"].strip()[:self.MAX_CONTENT_CHARS]}
                for row in candidates if isinstance(row["content"], str) and row["content"].strip()]
