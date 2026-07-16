"""Offline tests for embedding provider selection and local inference."""

import sys
from types import SimpleNamespace

import pytest

from coderAI.embeddings import create_embedding_provider, embedding_fingerprint
from coderAI.embeddings.local import SentenceTransformerEmbeddingProvider
from coderAI.embeddings.openai import create_embedding_provider as legacy_factory
from coderAI.system.config import Config


class _FakeEncoder:
    def __init__(self, model, **kwargs):
        self.model = model
        self.kwargs = kwargs
        self.calls = []

    def get_sentence_embedding_dimension(self):
        return 3

    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        return [[1.0, 0.0, 0.0] for _ in texts]


def test_auto_prefers_openai_when_key_is_configured():
    provider = create_embedding_provider(Config(openai_api_key="test-key"))

    assert provider is not None
    assert embedding_fingerprint(provider).to_dict() == {
        "backend": "openai",
        "model": "text-embedding-3-small",
        "dimension": 1536,
    }


def test_auto_selects_local_without_importing_optional_package(monkeypatch):
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)

    provider = create_embedding_provider(Config())

    assert isinstance(provider, SentenceTransformerEmbeddingProvider)
    assert "sentence_transformers" not in sys.modules


@pytest.mark.asyncio
async def test_local_provider_uses_mocked_package_offline(monkeypatch):
    fake_package = SimpleNamespace(SentenceTransformer=_FakeEncoder)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_package)
    provider = SentenceTransformerEmbeddingProvider(model="offline-model", device="cpu")

    vectors = await provider.embed(["first", "second"])

    assert vectors == [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    assert provider.dimension() == 3
    assert provider._encoder.model == "offline-model"
    assert provider._encoder.kwargs == {"device": "cpu"}
    texts, kwargs = provider._encoder.calls[0]
    assert texts == ["first", "second"]
    assert kwargs["normalize_embeddings"] is True


def test_missing_local_dependency_has_install_hint(monkeypatch):
    from coderAI.embeddings import local

    def missing_package(_name):
        raise ModuleNotFoundError("sentence_transformers")

    monkeypatch.setattr(local.importlib, "import_module", missing_package)
    provider = SentenceTransformerEmbeddingProvider()

    with pytest.raises(ImportError, match=r"coderAI\[local-embeddings\]"):
        provider.dimension()


def test_legacy_openai_factory_path_forwards_to_common_factory():
    provider = legacy_factory(Config(embedding_backend="local", embedding_model="offline"))

    assert isinstance(provider, SentenceTransformerEmbeddingProvider)
    assert provider.model == "offline"


def test_explicit_openai_backend_without_key_is_unavailable():
    assert create_embedding_provider(Config(embedding_backend="openai")) is None
