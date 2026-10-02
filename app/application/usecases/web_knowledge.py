"""Admit web knowledge only when it displaces a local retrieval result.

Relevance is not a compliance judgement. Web passages retain their provenance
and a trust discount on every query, including after they have been stored.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
from datetime import datetime, timezone
from typing import Any

from agentscope.message import TextBlock
from agentscope.rag import ApproxTokenChunker, Section
from app.application.usecases.web_product_policy import is_product_query, prepare_product_page


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text", ""))
    return str(getattr(content, "text", ""))


def _score(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Similarity scores must be finite")
    return result


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        raise ValueError("Embedding dimensions must match")
    left, right = list(map(_score, left)), list(map(_score, right))
    left_norm = math.sqrt(sum(x * x for x in left))
    right_norm = math.sqrt(sum(x * x for x in right))
    if not left_norm or not right_norm:
        raise ValueError("Zero embedding cannot be scored")
    return max(-1.0, min(1.0, _score(sum(
        a / left_norm * (b / right_norm) for a, b in zip(left, right)
    ))))


class WebKnowledgeService:
    def __init__(self, local_kb: Any, web_kb: Any, source: Any, *, weight: float = 0.8):
        self.weight = _score(weight)
        if not 0 < self.weight < 1:
            raise ValueError("Web weight must be strictly between 0 and 1")
        self.local_kb = local_kb
        self.web_kb = web_kb
        self.source = source
        self._lock = asyncio.Lock()

    def _hit(self, item: Any, source_type: str) -> dict:
        metadata = item.chunk.metadata or {}
        raw = _score(item.score)
        weight = self.weight if source_type == "web" else 1.0
        return {
            "content": _text(item.chunk.content),
            "source": metadata.get("url") or metadata.get("source") or item.document_id,
            "source_type": source_type,
            "score": weight * max(0.0, raw) if source_type == "web" else raw,
            "raw_score": raw,
            "weight": weight,
            "document_id": item.document_id,
            **{key: metadata[key] for key in ("url", "title", "fetched_at", "content_hash") if key in metadata},
        }

    async def search(self, question: str, top_k: int = 3, *, discover: bool = False) -> dict:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 10:
            raise ValueError("top_k must be an integer between 1 and 10")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must not be empty")
        # A shared service serializes admission so concurrent requests cannot
        # both insert the same deterministic document ID.
        async with self._lock:
            return await self._search(question.strip(), top_k, discover)

    async def _search(self, question: str, top_k: int, discover: bool) -> dict:
        try:
            local = [self._hit(hit, "local") for hit in await self.local_kb.search(
                queries=[question], top_k=top_k,
            )]
        except Exception:
            return {"insights": [], "gate": {"status": "local_unavailable", "threshold": None}, "admitted_count": 0}
        local.sort(key=lambda hit: hit["score"], reverse=True)
        local = local[:top_k]
        if not local:
            return {"insights": [], "gate": {"status": "local_empty", "threshold": None}, "admitted_count": 0}
        threshold = max(0.0, local[-1]["score"])
        result = {"gate": {"status": "active", "threshold": threshold, "weight": self.weight}, "admitted_count": 0}
        existing = []
        try:
            for item in await self.web_kb.search(queries=[question], top_k=top_k):
                hit = self._hit(item, "web")
                if is_product_query(question):
                    checked = prepare_product_page(hit, question)
                    if checked is None:
                        continue
                    hit["content"] = checked["content"]
                if hit["score"] > threshold:
                    existing.append(hit)
        except Exception:
            result["web_status"] = "stored_search_unavailable"
        candidates = []
        if discover and self.source is not None:
            try:
                candidates = await self._discover(question, threshold)
            except Exception:
                result["discovery_status"] = "unavailable"
        # Keep stored hits over identical discoveries and local hits on ties.
        pool = local + existing
        seen = {hit["document_id"] for hit in existing}
        for hit in candidates:
            if hit["document_id"] not in seen:
                pool.append(hit)
                seen.add(hit["document_id"])
        pool.sort(key=lambda hit: hit["score"], reverse=True)
        pool = self._unique_products(pool, question)
        winners = pool[:top_k]
        usable = list(local) + list(existing)
        for hit in winners:
            if "_chunk" not in hit:
                continue
            try:
                inserted = await self._persist(hit)
            except Exception:
                result["storage_status"] = "partial_failure"
                continue
            result["admitted_count"] += int(inserted)
            usable.append(hit)
        usable.sort(key=lambda hit: hit["score"], reverse=True)
        usable = self._unique_products(usable, question)
        result["insights"] = [{key: value for key, value in hit.items() if not key.startswith("_")}
                              for hit in usable[:top_k]]
        if is_product_query(question):
            result["product_status"] = (
                "eligible_products" if any(hit["source_type"] == "web" for hit in result["insights"])
                else "no_eligible_products"
            )
        return result

    @staticmethod
    def _unique_products(hits: list[dict], question: str) -> list[dict]:
        if not is_product_query(question):
            return hits
        seen, output = set(), []
        for hit in hits:
            url = hit.get("url") if hit["source_type"] == "web" else None
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            output.append(hit)
        return output

    async def _discover(self, question: str, threshold: float) -> list[dict]:
        pages = await self.source.search(question, max_results=5)
        chunker = ApproxTokenChunker(chunk_size=512, overlap=50)
        chunks = []
        seen_pages = set()
        for page in pages[:5]:
            if is_product_query(question):
                page = prepare_product_page(page, question)
                if page is None:
                    continue
            url = str(page.get("url", "")).strip()
            content = str(page.get("content", "")).strip()
            if not url or not content:
                continue
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if (url, content_hash) in seen_pages:
                continue
            seen_pages.add((url, content_hash))
            metadata = {
                "url": url, "source": url, "source_type": "web",
                "title": str(page.get("title", "")),
                "fetched_at": page.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
                "content_hash": content_hash,
            }
            chunks.extend(await chunker.chunk([
                Section(content=TextBlock(text=content), source=url, metadata=metadata),
            ]))
        if is_product_query(question):
            # A stored passage must stand on its own as purchase evidence.
            # Do not admit specification/review fragments without price or CTA.
            chunks = [chunk for chunk in chunks if prepare_product_page(
                {**chunk.metadata, "content": _text(chunk.content)}, question,
            ) is not None]
        if not chunks:
            return []
        # Bound a single discovery's embedding cost; excess passages are never
        # stored. Source fetching also applies its own document size limits.
        chunks = chunks[:64]
        model = self.local_kb.embedding_model
        query_response = await model([question])
        if len(query_response.embeddings) != 1:
            raise ValueError("Expected one query embedding")
        query_vector = query_response.embeddings[0]
        candidates = []
        for start in range(0, len(chunks), 10):
            batch = chunks[start:start + 10]
            response = await model([chunk.content for chunk in batch])
            if len(response.embeddings) != len(batch):
                raise ValueError("Embedding response length mismatch")
            for chunk, vector in zip(batch, response.embeddings):
                raw = _cosine(query_vector, vector)
                score = self.weight * max(0.0, raw)
                if score <= threshold:
                    continue
                metadata = chunk.metadata
                identity = "\0".join((metadata["url"], metadata["content_hash"], str(chunk.chunk_index)))
                document_id = "web_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
                candidates.append({
                    "content": _text(chunk.content), "source": metadata["url"],
                    "source_type": "web", "score": score, "raw_score": raw,
                    "weight": self.weight, "document_id": document_id,
                    **{key: metadata[key] for key in ("url", "title", "fetched_at", "content_hash")},
                    "_chunk": chunk,
                })
        return candidates

    async def _persist(self, hit: dict) -> bool:
        # Skip unnecessary re-embedding on sequential repeat requests. The web
        # store also uses deterministic point IDs, making races across workers
        # and retries after an uncertain write safe. Each admitted passage is
        # a separate document: rejected sibling passages can never slip in via
        # whole-page insertion.
        existing = {item.document_id for item in await self.web_kb.list_documents()}
        if hit["document_id"] in existing:
            return False
        await self.web_kb.insert_document(
            chunks=[hit["_chunk"]],
            document_id=hit["document_id"],
            document_metadata=dict(hit["_chunk"].metadata),
        )
        return True
