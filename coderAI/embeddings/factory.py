"""Factory for creating an embedding provider from config."""

from __future__ import annotations

import logging

from .base import EmbeddingProvider
from .openai import OpenAIEmbeddingProvider

logger = logging.getLogger(__name__)


def create_embedding_provider(config) -> EmbeddingProvider:
    """Create an embedding provider from the application config.

    Prefers OpenAI when an API key is present. Returns ``None`` if no
    embedding backend can be provisioned.
    """
    api_key = getattr(config, "openai_api_key", None)
    if api_key:
        return OpenAIEmbeddingProvider(api_key=api_key)
    return None
