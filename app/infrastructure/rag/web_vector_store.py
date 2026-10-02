"""Qdrant web storage with stable point IDs for concurrent admission."""
from __future__ import annotations

import json
import uuid

from agentscope.rag import QdrantStore, VectorRecord
from qdrant_client import models


class WebQdrantStore(QdrantStore):
    """Retain the AgentScope payload schema while making insertion an upsert.

    A document is a single admitted passage with a URL/content-derived ID.
    Concurrent workers and retried writes therefore address the same point,
    rather than creating duplicate vectors with AgentScope's random UUIDs.
    """

    async def insert(self, collection: str, records: list[VectorRecord]) -> None:
        if not records:
            return
        await self.get_client().upsert(
            collection_name=collection,
            wait=True,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
                        [collection, record.document_id, record.chunk.chunk_index],
                        ensure_ascii=False, separators=(",", ":"),
                    ))),
                    vector=record.vector,
                    payload={
                        "document_id": record.document_id,
                        "chunk": record.chunk.model_dump(mode="json"),
                    },
                )
                for record in records
            ],
        )
