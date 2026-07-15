"""OpenAI embeddings provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from coderAI.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)

# Small is cheap ($0.02 / 1M tokens), 1536 dims, and good enough for code search.
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
_MODEL_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class OpenAIEmbeddingProvider:
    """Generates embeddings via the OpenAI Embeddings API."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        base_url: str | None = None,
    ) -> None:
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    @property
    def backend(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, texts: Sequence[str]) -> List[List[float]]:
        resp = await self._client.embeddings.create(
            model=self._model,
            input=list(texts),
        )
        return [d.embedding for d in resp.data]

    def dimension(self) -> int:
        try:
            return _MODEL_DIMENSIONS[self._model]
        except KeyError as e:
            supported = ", ".join(sorted(_MODEL_DIMENSIONS))
            raise ValueError(
                f"Unknown dimension for OpenAI embedding model {self._model!r}. "
                f"Supported models: {supported}."
            ) from e


def create_embedding_provider(config: Any) -> Optional["EmbeddingProvider"]:
    """Compatibility shim forwarding to the common embedding factory."""
    from coderAI.embeddings import create_embedding_provider as common_factory

    return common_factory(config)
