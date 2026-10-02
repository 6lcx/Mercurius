"""Choose the same embedding backend for catalog and knowledge retrieval."""
from __future__ import annotations

from agentscope.credential import OpenAICredential
from agentscope.embedding import OpenAIEmbeddingModel

from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.infrastructure.embedding.local_embedding import LocalEmbeddingClient, LocalRagEmbeddingModel
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.settings import Settings


def build_embedding_client(settings: Settings) -> EmbeddingClient:
    if settings.embedding_provider == "local":
        return LocalEmbeddingClient(model=settings.embedding_model, dimensions=settings.embedding_dim)
    if settings.embedding_provider != "api":
        raise ValueError("EMBEDDING_PROVIDER must be api or local")
    return OpenAIEmbeddingClient(settings)


def build_rag_embedding_model(settings: Settings):
    if settings.embedding_provider == "local":
        return LocalRagEmbeddingModel(LocalEmbeddingClient(
            model=settings.embedding_model, dimensions=settings.embedding_dim,
        ))
    if settings.embedding_provider != "api":
        raise ValueError("EMBEDDING_PROVIDER must be api or local")
    return OpenAIEmbeddingModel(
        credential=OpenAICredential(
            api_key=settings.embedding_api_key, base_url=settings.embedding_base_url,
        ),
        model=settings.embedding_model,
        dimensions=settings.embedding_dim,
        pass_dimensions=False,
    )
