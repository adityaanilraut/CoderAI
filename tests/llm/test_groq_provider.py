"""Tests for the GroqProvider LLM provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.llm.groq import GroqProvider


class TestGroqProviderInit:
    def test_api_key_is_required(self):
        with pytest.raises(ValueError, match="API key is required"):
            GroqProvider(model="llama3-8b-8192", api_key=None)

    def test_default_temperature_and_max_tokens(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        assert provider.temperature == 0.7
        assert provider.max_tokens == 8192

    def test_kwargs_override_defaults(self):
        provider = GroqProvider(
            model="llama3-8b-8192",
            api_key="test-key",
            temperature=0.2,
            max_tokens=2048,
        )
        assert provider.temperature == 0.2
        assert provider.max_tokens == 2048

    def test_tracking_counters_initialised(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        assert provider.total_input_tokens == 0
        assert provider.total_output_tokens == 0


class TestModelMapping:
    def test_known_model_maps_to_self(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        assert provider.actual_model == "llama3-8b-8192"

    def test_unknown_model_passes_through(self):
        provider = GroqProvider(model="some-new-model", api_key="test-key")
        assert provider.actual_model == "some-new-model"

    def test_all_supported_models_map_correctly(self):
        for model in GroqProvider.SUPPORTED_MODELS:
            provider = GroqProvider(model=model, api_key="test-key")
            assert provider.actual_model == model


class TestChat:
    def _make_provider(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        return provider

    @pytest.mark.asyncio
    async def test_chat_sends_correct_params(self):
        provider = self._make_provider()
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(
            model_dump=lambda: {
                "choices": [{"message": {"content": "response"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            }
        )
        provider.client.chat.completions.create = mock_create

        result = await provider.chat([{"role": "user", "content": "hi"}])

        call_kwargs = mock_create.await_args.kwargs
        assert call_kwargs["model"] == "llama3-8b-8192"
        assert call_kwargs["messages"] == [{"role": "user", "content": "hi"}]
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 8192
        assert result["choices"][0]["message"]["content"] == "response"

    @pytest.mark.asyncio
    async def test_chat_tracks_usage(self):
        provider = self._make_provider()
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(
            model_dump=lambda: {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 15},
            }
        )
        provider.client.chat.completions.create = mock_create

        await provider.chat([{"role": "user", "content": "hi"}])
        assert provider.total_input_tokens == 30
        assert provider.total_output_tokens == 15

    @pytest.mark.asyncio
    async def test_chat_includes_tools(self):
        provider = self._make_provider()
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(
            model_dump=lambda: {
                "choices": [{"message": {"content": "done"}}],
                "usage": {},
            }
        )
        provider.client.chat.completions.create = mock_create

        tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
        await provider.chat([{"role": "user", "content": "find"}], tools=tools)

        call_kwargs = mock_create.await_args.kwargs
        assert call_kwargs["tools"] == tools
        assert call_kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_chat_handles_api_error(self):
        provider = self._make_provider()
        provider.client.chat.completions.create = AsyncMock(
            side_effect=Exception("Rate limit exceeded")
        )

        with pytest.raises(RuntimeError, match="Groq API error"):
            await provider.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_chat_override_temperature(self):
        provider = self._make_provider()
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(
            model_dump=lambda: {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            }
        )
        provider.client.chat.completions.create = mock_create

        await provider.chat([{"role": "user", "content": "hi"}], temperature=0.1)
        assert mock_create.await_args.kwargs["temperature"] == 0.1


class TestStream:
    def _make_provider(self):
        return GroqProvider(model="llama3-8b-8192", api_key="test-key")

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self):
        provider = self._make_provider()

        async def mock_stream():
            for data in [
                {
                    "choices": [{"delta": {"content": "Hello"}}],
                    "x_groq": {"usage": {"prompt_tokens": 5}},
                },
                {
                    "choices": [{"delta": {"content": " world"}}],
                    "x_groq": {"usage": {"completion_tokens": 3}},
                },
            ]:
                yield MagicMock(model_dump=lambda: data)

        provider.client.chat.completions.create = AsyncMock(return_value=mock_stream())

        chunks = []
        async for chunk in provider.stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "Hello"
        assert chunks[1]["choices"][0]["delta"]["content"] == " world"

    @pytest.mark.asyncio
    async def test_stream_tracks_groq_usage(self):
        provider = self._make_provider()

        async def mock_stream():
            data = {
                "choices": [{"delta": {"content": "x"}}],
                "x_groq": {"usage": {"prompt_tokens": 10, "completion_tokens": 7}},
            }
            yield MagicMock(model_dump=lambda: data)

        provider.client.chat.completions.create = AsyncMock(return_value=mock_stream())

        async for _ in provider.stream([{"role": "user", "content": "hi"}]):
            pass

        assert provider.total_input_tokens == 10
        assert provider.total_output_tokens == 7

    @pytest.mark.asyncio
    async def test_stream_includes_tools_in_params(self):
        provider = self._make_provider()

        async def mock_stream():
            yield MagicMock(model_dump=lambda: {"choices": [], "x_groq": {}})

        provider.client.chat.completions.create = AsyncMock(return_value=mock_stream())

        tools = [{"type": "function", "function": {"name": "tool1", "parameters": {}}}]
        async for _ in provider.stream([{"role": "user", "content": "test"}], tools=tools):
            pass

        call_kwargs = provider.client.chat.completions.create.await_args.kwargs
        assert call_kwargs["tools"] == tools
        assert call_kwargs["stream"] is True

    @pytest.mark.asyncio
    async def test_stream_handles_api_error(self):
        provider = self._make_provider()
        provider.client.chat.completions.create = AsyncMock(
            side_effect=Exception("Connection lost")
        )

        with pytest.raises(RuntimeError, match="Groq API streaming error"):
            async for _ in provider.stream([{"role": "user", "content": "hi"}]):
                pass


class TestCountTokens:
    def test_count_tokens_approximates(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        assert provider.count_tokens("hello world") == 3  # ceil(11/4) == 3
        assert provider.count_tokens("") == 0
        assert provider.count_tokens("abcd" * 10) == 10  # ceil(40/4) == 10


class TestGetCost:
    def test_get_cost_zero_when_no_usage(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        cost = provider.get_cost()
        assert cost["input_tokens"] == 0
        assert cost["output_tokens"] == 0
        assert cost["total_tokens"] == 0
        assert cost["input_cost"] == 0
        assert cost["output_cost"] == 0
        assert cost["total_cost"] == 0
        assert cost["currency"] == "USD"

    def test_get_cost_calculates_from_tracked_usage(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        provider.total_input_tokens = 1_000_000
        provider.total_output_tokens = 1_000_000
        cost = provider.get_cost()
        # llama3-8b-8192 pricing: input=0.05, output=0.08
        assert cost["input_cost"] == pytest.approx(0.05)
        assert cost["output_cost"] == pytest.approx(0.08)
        assert cost["total_cost"] == pytest.approx(0.13)

    def test_get_cost_uses_actual_model_for_pricing(self):
        provider = GroqProvider(model="openai/gpt-oss-20b", api_key="test-key")
        provider.total_input_tokens = 1_000_000
        provider.total_output_tokens = 1_000_000
        cost = provider.get_cost()
        # openai/gpt-oss-20b pricing: input=0.075, output=0.30
        assert cost["input_cost"] == pytest.approx(0.075)
        assert cost["output_cost"] == pytest.approx(0.30)
        assert cost["total_cost"] == pytest.approx(0.375)

    def test_get_cost_with_partial_tokens(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        provider.total_input_tokens = 500_000
        provider.total_output_tokens = 250_000
        cost = provider.get_cost()
        assert cost["input_cost"] == pytest.approx(0.025)  # 0.5 * 0.05
        assert cost["output_cost"] == pytest.approx(0.02)  # 0.25 * 0.08
        assert cost["total_cost"] == pytest.approx(0.045)


class TestGetModelInfo:
    def test_get_model_info_returns_full_info(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        provider.total_input_tokens = 100
        provider.total_output_tokens = 50
        info = provider.get_model_info()
        assert info["provider"] == "GroqProvider"
        assert info["model"] == "llama3-8b-8192"
        assert info["total_input_tokens"] == 100
        assert info["total_output_tokens"] == 50
        assert info["total_tokens"] == 150
        assert "cost" in info


class TestClose:
    @pytest.mark.asyncio
    async def test_close_calls_client_close(self):
        provider = GroqProvider(model="llama3-8b-8192", api_key="test-key")
        provider.client.close = AsyncMock()
        await provider.close()
        provider.client.close.assert_awaited_once()
