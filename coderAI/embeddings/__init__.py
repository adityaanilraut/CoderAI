"""Embedding providers for semantic code search.

Each provider wraps a remote or local embeddings API behind a uniform interface
so the indexer and search tool don't care where the vectors come from.
"""

from coderAI.embeddings.openai import OpenAIEmbeddingProvider, create_embedding_provider

__all__ = [
    "OpenAIEmbeddingProvider",
    "create_embedding_provider",
]
