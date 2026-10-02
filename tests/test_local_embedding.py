"""Local embedding contracts without downloading weights or requesting model APIs."""
from dataclasses import replace

import httpx
import pytest
from agentscope.message import TextBlock

from app.infrastructure.embedding import local_embedding as mod
from app.infrastructure.embedding.factory import build_embedding_client, build_rag_embedding_model
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from tests.test_retrieval import _settings


class FakeBackend:
    def __init__(self, dimensions=512):
        self.dimensions = dimensions
        self.seen = []

    def embed(self, texts, **kwargs):
        self.seen.extend(texts)
        return [[float(len(text))] + [0.0] * (self.dimensions - 1) for text in texts]


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(mod, "_get_model", lambda *args, **kwargs: fake)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: pytest.fail("local inference must not request a remote API"))
    return fake


async def test_local_batch_512_and_single_without_api_key(backend):
    client = mod.LocalEmbeddingClient()
    rows = await client.embed_batch(["中文", "english"])
    assert len(rows) == 2
    assert all(len(vector) == 512 for vector in rows)
    assert rows[0][0] == 2
    assert await client.embed("单条") == rows[0]
    assert backend.seen == ["中文", "english", "单条"]


async def test_empty_batch_does_not_load_backend(monkeypatch):
    monkeypatch.setattr(mod, "_get_model", lambda *args, **kwargs: pytest.fail("empty batch must not load weights"))
    assert await mod.LocalEmbeddingClient().embed_batch([]) == []


async def test_rag_adapter_accepts_textblock_and_string(backend):
    adapter = mod.LocalRagEmbeddingModel(client=mod.LocalEmbeddingClient())
    response = await adapter(["question", TextBlock(text="知识片段")])
    assert adapter.dimensions == 512
    assert adapter.supports_multimodal is False
    assert len(response.embeddings) == 2
    assert all(len(row) == 512 for row in response.embeddings)
    assert backend.seen == ["question", "知识片段"]


@pytest.mark.parametrize("dimensions", [0, 8, 1024])
def test_local_dimension_mismatch_rejected_before_loading(dimensions, monkeypatch):
    monkeypatch.setattr(mod, "_get_model", lambda *args, **kwargs: pytest.fail("invalid configuration must not load weights"))
    with pytest.raises(ValueError):
        mod.LocalEmbeddingClient(dimensions=dimensions)


async def test_backend_wrong_vector_shape_rejected(monkeypatch):
    monkeypatch.setattr(mod, "_get_model", lambda *args, **kwargs: FakeBackend(8))
    with pytest.raises(ValueError):
        await mod.LocalEmbeddingClient().embed("question")


def test_default_provider_keeps_api_client_and_rag_model(tmp_path):
    from agentscope.embedding import OpenAIEmbeddingModel
    settings = replace(_settings(tmp_path), embedding_api_key="test-only-key", embedding_base_url="https://example.invalid/v1")
    assert settings.embedding_provider == "api"
    assert isinstance(build_embedding_client(settings), OpenAIEmbeddingClient)
    assert isinstance(build_rag_embedding_model(settings), OpenAIEmbeddingModel)


async def test_local_factory_and_both_knowledge_bases_work_without_embedding_key(tmp_path, backend):
    from app.infrastructure.rag.category_knowledge import build_category_knowledge_base, build_web_knowledge_base
    settings = replace(_settings(tmp_path), embedding_provider="local", embedding_model=mod.LOCAL_MODEL, embedding_dim=512, embedding_api_key="", embedding_base_url="")
    assert isinstance(build_embedding_client(settings), mod.LocalEmbeddingClient)
    local, web = build_category_knowledge_base(settings), build_web_knowledge_base(settings)
    try:
        for kb in (local, web):
            assert isinstance(kb.embedding_model, mod.LocalRagEmbeddingModel)
            response = await kb.embedding_model(["query"])
            assert len(response.embeddings[0]) == 512
    finally:
        await local.vector_store.__aexit__(None, None, None)
        await web.vector_store.__aexit__(None, None, None)


def test_local_settings_reject_incompatible_dimension(tmp_path):
    with pytest.raises(ValueError):
        replace(_settings(tmp_path), embedding_provider="local", embedding_model=mod.LOCAL_MODEL, embedding_dim=1024)


def test_settings_load_selects_local_defaults(monkeypatch, tmp_path):
    from app.infrastructure import settings as settings_module
    env = {"LLM_API_KEY": "test-only", "EMBEDDING_PROVIDER": "local", "DATA_DIR": str(tmp_path)}
    monkeypatch.setattr(settings_module.os, "getenv", lambda name, default=None: env.get(name, default))
    settings = settings_module.load_settings()
    assert settings.embedding_provider == "local"
    assert settings.embedding_model == mod.LOCAL_MODEL
    assert settings.embedding_dim == 512


def test_unknown_provider_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        replace(_settings(tmp_path), embedding_provider="typo")


async def test_nonfinite_backend_values_are_rejected(monkeypatch):
    class NonfiniteBackend:
        def embed(self, texts, **kwargs):
            return [[float("nan")] + [0.0] * 511 for text in texts]
    monkeypatch.setattr(mod, "_get_model", lambda *args, **kwargs: NonfiniteBackend())
    with pytest.raises(ValueError):
        await mod.LocalEmbeddingClient().embed("question")
