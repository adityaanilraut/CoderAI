"""Embedding provider protocol and factory for semantic code search."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from coderAI.embeddings.openai import OpenAIEmbeddingProvider


@dataclass(frozen=True)
class EmbeddingFingerprint:
    """Identity of vectors that may safely share an index."""

    backend: str
    model: str
    dimension: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Common interface implemented by remote and local embedding providers."""

    @property
    def backend(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> List[List[float]]: ...

    def dimension(self) -> int: ...


def embedding_fingerprint(provider: EmbeddingProvider) -> EmbeddingFingerprint:
    """Return the index compatibility identity for *provider*."""
    dimension = provider.dimension()
    if dimension <= 0:
        raise ValueError(f"Embedding provider returned invalid dimension {dimension}")
    return EmbeddingFingerprint(
        backend=provider.backend,
        model=provider.model,
        dimension=dimension,
    )


def create_embedding_provider(config: Any) -> Optional[EmbeddingProvider]:
    """Create the configured provider without importing optional local packages.

    ``auto`` prefers OpenAI when a key is configured and otherwise selects the
    local backend. The local model and sentence-transformers package are loaded
    only when embeddings or dimension metadata are first requested.
    """
    requested_backend = str(getattr(config, "embedding_backend", "auto")).lower()
    api_key = getattr(config, "openai_api_key", None)
    backend = "openai" if requested_backend == "auto" and api_key else requested_backend
    if backend == "auto":
        backend = "local"

    configured_model = getattr(config, "embedding_model", None)
    if backend == "openai":
        if not api_key:
            return None
        from coderAI.embeddings.openai import DEFAULT_OPENAI_EMBEDDING_MODEL

        return OpenAIEmbeddingProvider(
            api_key=api_key,
            model=configured_model or DEFAULT_OPENAI_EMBEDDING_MODEL,
        )
    if backend == "local":
        from coderAI.embeddings.local import (
            DEFAULT_LOCAL_EMBEDDING_MODEL,
            SentenceTransformerEmbeddingProvider,
        )

        return SentenceTransformerEmbeddingProvider(
            model=configured_model or DEFAULT_LOCAL_EMBEDDING_MODEL,
            device=getattr(config, "embedding_device", None),
        )
    raise ValueError(
        f"Unknown embedding backend {requested_backend!r}; expected auto, openai, or local"
    )


__all__ = [
    "EmbeddingFingerprint",
    "EmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "create_embedding_provider",
    "embedding_fingerprint",
]
