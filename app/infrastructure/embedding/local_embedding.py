"""Free local CPU embeddings shared by product retrieval and both RAG stores.

Model loading is lazy: importing/building the app does not download weights.
The first embedding downloads the public ONNX model into data/models; later
runs reuse it. All text uses the same encode path without query-only prefixes.
"""
from __future__ import annotations

import asyncio
import math
import threading
from pathlib import Path
from typing import Any

from agentscope.embedding import EmbeddingResponse
from agentscope.message import TextBlock

from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.infrastructure.settings import PROJECT_ROOT

LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"
LOCAL_DIMENSIONS = 512
_models: dict[tuple[str, str], Any] = {}
_load_lock = threading.Lock()
_inference_lock = threading.Lock()


def _get_model(model_name: str, cache_dir: str) -> Any:
    key = (model_name, cache_dir)
    with _load_lock:
        if key not in _models:
            from fastembed import TextEmbedding

            # FastEmbed's official archive can be pre-downloaded when the
            # Hugging Face endpoint is unavailable. Load that complete local
            # model directly instead of probing a remote repository again.
            extracted = Path(cache_dir) / "fast-bge-small-zh-v1.5"
            local_options = {}
            if (extracted / "model_optimized.onnx").is_file():
                local_options = {
                    "specific_model_path": str(extracted),
                    "local_files_only": True,
                }
            _models[key] = TextEmbedding(
                model_name=model_name,
                cache_dir=cache_dir,
                threads=2,
                providers=["CPUExecutionProvider"],
                **local_options,
            )
        return _models[key]


class LocalEmbeddingClient(EmbeddingClient):
    def __init__(
        self,
        model: str = LOCAL_MODEL,
        dimensions: int = LOCAL_DIMENSIONS,
        cache_dir: Path | str | None = None,
    ) -> None:
        if model != LOCAL_MODEL or dimensions != LOCAL_DIMENSIONS:
            raise ValueError(f"Local embedding requires {LOCAL_MODEL} and 512 dimensions")
        self.model = model
        self.dimensions = dimensions
        self.cache_dir = str(Path(cache_dir or PROJECT_ROOT / "data" / "models").resolve())

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if any(not isinstance(text, str) for text in texts):
            raise TypeError("Local embedding accepts text strings only")
        if not texts:
            return []
        return await asyncio.to_thread(self._encode, list(texts))

    def _encode(self, texts: list[str]) -> list[list[float]]:
        # Serialize use of the shared ONNX session across request threads;
        # ONNX itself uses two CPU threads, with no multiprocessing workers.
        with _inference_lock:
            model = _get_model(self.model, self.cache_dir)
            vectors = [list(map(float, vector)) for vector in model.embed(
                texts, batch_size=32, parallel=None,
            )]
        if len(vectors) != len(texts) or any(
            len(vector) != self.dimensions or not all(math.isfinite(x) for x in vector)
            for vector in vectors
        ):
            raise ValueError("Local embedding returned invalid vector dimensions or values")
        return vectors


class LocalRagEmbeddingModel:
    """AgentScope KnowledgeBase's text embedding protocol, without credentials."""

    supports_multimodal = False
    context_size = 512
    batch_size = 32

    def __init__(self, client: LocalEmbeddingClient | None = None) -> None:
        self.client = client or LocalEmbeddingClient()
        self.model = self.client.model
        self.dimensions = self.client.dimensions

    async def __call__(self, inputs: list[str | TextBlock], **kwargs: Any) -> EmbeddingResponse:
        texts = [item.text if isinstance(item, TextBlock) else item for item in inputs]
        return EmbeddingResponse(embeddings=await self.client.embed_batch(texts))
