"""Embedding providers for semantic code search.

Each provider wraps a remote or local embeddings API behind a uniform interface
so the indexer and search tool don't care where the vectors come from.
"""

from coderAI.embeddings.base import EmbeddingProvider
from coderAI.embeddings.factory import create_embedding_provider
from coderAI.embeddings.openai import OpenAIEmbeddingProvider

__all__ = [
    "EmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "create_embedding_provider",
]
