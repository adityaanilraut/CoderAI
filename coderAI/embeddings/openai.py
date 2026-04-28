"""OpenAI embeddings provider."""

from __future__ import annotations

import logging
from typing import List

from openai import AsyncOpenAI

from .base import EmbeddingProvider

logger = logging.getLogger(__name__)

# Small is cheap ($0.02 / 1M tokens), 1536 dims, and good enough for code search.
_DEFAULT_MODEL = "text-embedding-3-small"
_DEFAULT_DIMENSIONS = 1536


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Generates embeddings via the OpenAI Embeddings API."""

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str | None = None,
    ) -> None:
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def embed(self, texts: List[str]) -> List[List[float]]:
        resp = await self._client.embeddings.create(
            model=self._model,
            input=texts,
        )
        return [d.embedding for d in resp.data]

    def dimension(self) -> int:
        return _DEFAULT_DIMENSIONS
