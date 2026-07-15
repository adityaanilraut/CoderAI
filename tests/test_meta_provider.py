"""Tests for the Meta Model API LLM provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.llm.factory import create_provider, get_all_model_ids, get_models_by_provider
from coderAI.llm.meta import MetaProvider


def _config(**overrides):
    base = dict(
        meta_api_key="test-meta-key-1234567890",
        temperature=0.7,
        max_tokens=8192,
        reasoning_effort="medium",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestMetaProviderInit:
    def test_api_key_is_required(self):
        with pytest.raises(ValueError, match="API key is required"):
            MetaProvider(model="muse-spark-1.1", api_key=None)

    def test_base_url_and_defaults(self):
        provider = MetaProvider(model="muse-spark-1.1", api_key="test-key")
        assert provider.temperature == 0.7
        assert provider.max_tokens == 8192
        assert str(provider.client.base_url).rstrip("/") == "https://api.meta.ai/v1"


class TestModelMapping:
    def test_canonical_model(self):
        provider = MetaProvider(model="muse-spark-1.1", api_key="test-key")
        assert provider.actual_model == "muse-spark-1.1"

    def test_muse_alias(self):
        provider = MetaProvider(model="muse", api_key="test-key")
        assert provider.actual_model == "muse-spark-1.1"

    def test_muse_spark_alias(self):
        provider = MetaProvider(model="muse-spark", api_key="test-key")
        assert provider.actual_model == "muse-spark-1.1"

    def test_case_insensitive(self):
        provider = MetaProvider(model="Muse-Spark-1.1", api_key="test-key")
        assert provider.actual_model == "muse-spark-1.1"

    def test_all_supported_models_map_correctly(self):
        for alias, expected in MetaProvider.SUPPORTED_MODELS.items():
            provider = MetaProvider(model=alias, api_key="test-key")
            assert provider.actual_model == expected


class TestReasoningEffort:
    def _chat_kwargs(self, *, reasoning_effort: str):
        provider = MetaProvider(
            model="muse-spark-1.1",
            api_key="test-key",
            reasoning_effort=reasoning_effort,
        )
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(
            model_dump=lambda: {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            }
        )
        provider.client.chat.completions.create = mock_create
        return provider, mock_create

    @pytest.mark.asyncio
    async def test_omits_reasoning_effort_when_none(self):
        provider, mock_create = self._chat_kwargs(reasoning_effort="none")
        await provider.chat([{"role": "user", "content": "hi"}])
        assert "reasoning_effort" not in mock_create.await_args.kwargs

    @pytest.mark.asyncio
    async def test_passes_reasoning_effort_when_set(self):
        provider, mock_create = self._chat_kwargs(reasoning_effort="high")
        await provider.chat([{"role": "user", "content": "hi"}])
        assert mock_create.await_args.kwargs["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_chat_sends_correct_model(self):
        provider, mock_create = self._chat_kwargs(reasoning_effort="medium")
        await provider.chat([{"role": "user", "content": "hi"}])
        assert mock_create.await_args.kwargs["model"] == "muse-spark-1.1"
        assert mock_create.await_args.kwargs["messages"] == [{"role": "user", "content": "hi"}]


class TestFactoryRouting:
    def test_muse_spark_1_1(self):
        provider = create_provider("muse-spark-1.1", _config())
        assert isinstance(provider, MetaProvider)
        assert provider.actual_model == "muse-spark-1.1"

    def test_muse_alias(self):
        provider = create_provider("muse", _config())
        assert isinstance(provider, MetaProvider)
        assert provider.actual_model == "muse-spark-1.1"

    def test_meta_prefix(self):
        provider = create_provider("meta/muse-spark-1.1", _config())
        assert isinstance(provider, MetaProvider)
        assert provider.actual_model == "muse-spark-1.1"

    def test_listed_in_model_ids(self):
        ids = get_all_model_ids()
        assert "muse-spark-1.1" in ids
        assert "muse" in ids
        assert "muse-spark" in ids

    def test_listed_by_provider(self):
        groups = get_models_by_provider()
        labels = [label for label, _models, _req in groups]
        assert "Meta Provider" in labels
        meta = next(g for g in groups if g[0] == "Meta Provider")
        assert "muse-spark-1.1" in meta[1]
