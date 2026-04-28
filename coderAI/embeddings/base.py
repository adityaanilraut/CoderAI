"""Abstract base class for embedding providers."""

from abc import ABC, abstractmethod
from typing import List


class EmbeddingProvider(ABC):
    """Interface for generating vector embeddings from text.

    All providers return a list of floats per input string so callers
    never need to know the underlying API.
    """

    @abstractmethod
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for a batch of texts.

        Args:
            texts: One or more strings to embed.

        Returns:
            A list of embedding vectors, one per input string, in the same order.
        """
        ...

    @abstractmethod
    def dimension(self) -> int:
        """Return the dimensionality of the embeddings produced by this provider."""
        ...
