"""Optional local embeddings powered by sentence-transformers."""

from __future__ import annotations

import asyncio
import importlib
from typing import Any, List, Optional, Sequence

DEFAULT_LOCAL_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_INSTALL_HINT = (
    "Local embeddings require the optional sentence-transformers dependency. "
    "Install it with: pip install 'coderAI[local-embeddings]'"
)


class SentenceTransformerEmbeddingProvider:
    """Generate embeddings on the local machine without a hosted API."""

    def __init__(self, model: str = DEFAULT_LOCAL_EMBEDDING_MODEL, device: Optional[str] = None):
        self._model_name = model
        self._device = device
        self._encoder: Optional[Any] = None
        self._dimension: Optional[int] = None

    @property
    def backend(self) -> str:
        return "local"

    @property
    def model(self) -> str:
        return self._model_name

    def _load_encoder(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        try:
            package = importlib.import_module("sentence_transformers")
        except (ImportError, ModuleNotFoundError) as e:
            raise ImportError(_INSTALL_HINT) from e

        kwargs = {"device": self._device} if self._device else {}
        self._encoder = package.SentenceTransformer(self._model_name, **kwargs)
        return self._encoder

    def dimension(self) -> int:
        if self._dimension is None:
            value = self._load_encoder().get_sentence_embedding_dimension()
            if not isinstance(value, int) or value <= 0:
                raise ValueError(
                    f"Local embedding model {self._model_name!r} returned invalid dimension {value!r}"
                )
            self._dimension = value
        return self._dimension

    def _embed_sync(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        vectors = self._load_encoder().encode(
            list(texts),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        result = [
            vector.tolist() if hasattr(vector, "tolist") else list(vector) for vector in vectors
        ]
        expected_dimension = self.dimension()
        if len(result) != len(texts):
            raise ValueError(
                f"Local embedding model returned {len(result)} vectors for {len(texts)} texts"
            )
        if any(len(vector) != expected_dimension for vector in result):
            raise ValueError(
                f"Local embedding model returned a vector with a dimension other than "
                f"{expected_dimension}"
            )
        return result

    async def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return await asyncio.to_thread(self._embed_sync, texts)
