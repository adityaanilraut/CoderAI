"""Tests for the OpenAICompatibleLocalProvider and its subclasses."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.llm.local_base import OpenAICompatibleLocalProvider
from coderAI.llm.lmstudio import LMStudioProvider
from coderAI.llm.ollama import OllamaProvider


class TestInheritance:
    def test_lmstudio_inherits_from_local_base(self):
        provider = LMStudioProvider()
        assert isinstance(provider, OpenAICompatibleLocalProvider)

    def test_ollama_inherits_from_local_base(self):
        provider = OllamaProvider()
        assert isinstance(provider, OpenAICompatibleLocalProvider)

    def test_default_endpoints_are_set(self):
        lm = LMStudioProvider()
        ollama = OllamaProvider()
        assert "1234" in lm.endpoint
        assert "11434" in ollama.endpoint


class TestOpenAICompatibleLocalProvider:
    def _make_provider(self):
        return OpenAICompatibleLocalProvider(model="test-model", endpoint="http://localhost:8080")

    def test_constructor_sets_endpoint_with_v1_suffix(self):
        provider = self._make_provider()
        assert provider.endpoint == "http://localhost:8080/v1"

    def test_constructor_preserves_v1_suffix(self):
        provider = OpenAICompatibleLocalProvider(
            model="test-model", endpoint="http://localhost:8080/v1"
        )
        assert provider.endpoint == "http://localhost:8080/v1"

    def test_temperature_and_max_tokens_defaults(self):
        provider = self._make_provider()
        assert provider.temperature == 0.7
        assert provider.max_tokens == 8192

    def test_kwargs_override_defaults(self):
        provider = OpenAICompatibleLocalProvider(
            model="test-model",
            endpoint="http://localhost:8080",
            temperature=1.2,
            max_tokens=4096,
        )
        assert provider.temperature == 1.2
        assert provider.max_tokens == 4096

    def test_count_tokens_approximates(self):
        provider = self._make_provider()
        assert provider.count_tokens("hello world!") == 3  # 12 // 4
        assert provider.count_tokens("") == 0

    def test_supports_tools(self):
        provider = self._make_provider()
        assert provider.supports_tools() is True

    def test_get_cost_returns_zero_cost(self):
        provider = self._make_provider()
        cost = provider.get_cost()
        assert cost["input_cost"] == 0
        assert cost["output_cost"] == 0
        assert cost["total_cost"] == 0
        assert cost["currency"] == "USD"
        assert cost["model"] == "test-model"

    def test_get_cost_includes_token_counts(self):
        provider = self._make_provider()
        provider.total_input_tokens = 100
        provider.total_output_tokens = 50
        cost = provider.get_cost()
        assert cost["input_tokens"] == 100
        assert cost["output_tokens"] == 50
        assert cost["total_tokens"] == 150

    def test_get_model_info_returns_full_info(self):
        provider = self._make_provider()
        provider.total_input_tokens = 200
        provider.total_output_tokens = 80
        info = provider.get_model_info()
        assert info["provider"] == "OpenAICompatibleLocalProvider"
        assert info["model"] == "test-model"
        assert info["endpoint"] == "http://localhost:8080/v1"
        assert info["total_input_tokens"] == 200
        assert info["total_output_tokens"] == 80
        assert info["total_tokens"] == 280

    def test_get_provider_label_strips_provider_suffix(self):
        provider = self._make_provider()
        assert provider._get_provider_label() == "OpenAICompatibleLocal"

    def test_get_url_returns_chat_completions(self):
        provider = self._make_provider()
        assert provider._get_url().endswith("/chat/completions")

    def test_track_usage_updates_counters(self):
        provider = self._make_provider()
        provider._track_usage({"prompt_tokens": 50, "completion_tokens": 30})
        assert provider.total_input_tokens == 50
        assert provider.total_output_tokens == 30

    def test_track_usage_handles_empty_usage(self):
        provider = self._make_provider()
        provider._track_usage({})
        assert provider.total_input_tokens == 0
        assert provider.total_output_tokens == 0

    def test_build_payload_includes_model_and_messages(self):
        provider = self._make_provider()
        payload = provider._build_payload([{"role": "user", "content": "hi"}])
        assert payload["model"] == "test-model"
        assert payload["messages"] == [{"role": "user", "content": "hi"}]
        assert "stream" not in payload

    def test_build_payload_sets_stream_flag(self):
        provider = self._make_provider()
        payload = provider._build_payload([], stream=True)
        assert payload["stream"] is True

    def test_build_payload_includes_tools(self):
        provider = self._make_provider()
        tools = [{"type": "function", "function": {"name": "test_tool", "parameters": {}}}]
        payload = provider._build_payload([], tools=tools)
        assert payload["tools"] == tools
        assert payload["tool_choice"] == "auto"

    def test_default_transform_chat_response_is_identity(self):
        provider = self._make_provider()
        result = {"choices": [{"message": {"content": "hello"}}]}
        assert provider._transform_chat_response(result) == result

    def test_default_transform_stream_chunk_is_identity(self):
        provider = self._make_provider()
        chunk = {"choices": [{"delta": {"content": "hi"}}]}
        assert provider._transform_stream_chunk(chunk) == chunk

    @pytest.mark.asyncio
    async def test_chat_mocked_response(self):
        provider = self._make_provider()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "hello from local"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

        mock_session_cls = MagicMock()
        mock_session_cls.post.return_value.__aenter__.return_value = mock_response
        mock_session_cls.closed = True

        with patch.object(provider, "_get_session", return_value=mock_session_cls):
            result = await provider.chat([{"role": "user", "content": "hi"}])
        assert result["choices"][0]["message"]["content"] == "hello from local"
        assert provider.total_input_tokens == 10
        assert provider.total_output_tokens == 5

    @pytest.mark.asyncio
    async def test_chat_handles_malformed_json(self):
        provider = self._make_provider()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json = AsyncMock(side_effect=ValueError("bad json"))

        mock_session_cls = MagicMock()
        mock_session_cls.post.return_value.__aenter__.return_value = mock_response
        mock_session_cls.closed = True

        with patch.object(provider, "_get_session", return_value=mock_session_cls):
            with pytest.raises(RuntimeError, match="malformed JSON"):
                await provider.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_stream_yields_transformed_chunks(self):
        provider = self._make_provider()
        sse_data = (
            b'data: {"choices":[{"delta":{"content":"Hello"}}],"usage":{"prompt_tokens":5}}'
            b"\n\n"
            b'data: {"choices":[{"delta":{"content":" world"}}],"usage":{"completion_tokens":2}}'
            b"\n\n"
            b"data: [DONE]\n\n"
        )
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.raise_for_status = lambda: None
        mock_response.content.__aiter__.return_value = [sse_data]

        mock_session_cls = MagicMock()
        mock_session_cls.post = AsyncMock(return_value=mock_response)
        mock_session_cls.closed = True

        with patch.object(provider, "_get_session", return_value=mock_session_cls):
            chunks = []
            async for chunk in provider.stream([{"role": "user", "content": "hi"}]):
                chunks.append(chunk)

        assert len(chunks) == 2
        assert provider.total_input_tokens == 5
        assert provider.total_output_tokens == 2

    @pytest.mark.asyncio
    async def test_stream_skips_malformed_sse_lines(self):
        provider = self._make_provider()
        sse_data = b'data: {"choices":[{"delta":{"content":"ok"}}]}\ndata: not-json\ndata: [DONE]\n'
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.raise_for_status = lambda: None
        mock_response.content.__aiter__.return_value = [sse_data]

        mock_session_cls = MagicMock()
        mock_session_cls.post = AsyncMock(return_value=mock_response)
        mock_session_cls.closed = True

        with patch.object(provider, "_get_session", return_value=mock_session_cls):
            chunks = []
            async for chunk in provider.stream([{"role": "user", "content": "hi"}]):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "ok"

    @pytest.mark.asyncio
    async def test_close_cleans_up_session(self):
        provider = self._make_provider()
        mock_session = AsyncMock()
        mock_session.closed = False
        provider._session = mock_session

        await provider.close()
        mock_session.close.assert_awaited_once()


class TestOllamaProvider:
    def test_default_model_and_endpoint(self):
        provider = OllamaProvider()
        assert provider.model == "llama3"
        assert "11434" in provider.endpoint

    def test_transform_chat_response_injects_reasoning(self):
        provider = OllamaProvider()
        result = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The answer is 42",
                        "reasoning": "Let me think...",
                    }
                }
            ]
        }
        transformed = provider._transform_chat_response(result)
        message = transformed["choices"][0]["message"]
        assert "reasoning" not in message
        assert "<think>\nLet me think...\n</think>\n\nThe answer is 42" in message["content"]

    def test_transform_chat_response_no_reasoning_no_change(self):
        provider = OllamaProvider()
        result = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "plain answer",
                    }
                }
            ]
        }
        transformed = provider._transform_chat_response(result)
        message = transformed["choices"][0]["message"]
        assert "<think>" not in message["content"]
        assert message["content"] == "plain answer"

    def test_transform_chat_response_empty_choices(self):
        provider = OllamaProvider()
        result = {"choices": []}
        transformed = provider._transform_chat_response(result)
        assert transformed == {"choices": []}

    def test_transform_stream_chunk_moves_reasoning_delta(self):
        provider = OllamaProvider()
        chunk = {
            "choices": [
                {
                    "delta": {
                        "reasoning": "hmm...",
                        "content": "result",
                    }
                }
            ]
        }
        transformed = provider._transform_stream_chunk(chunk)
        delta = transformed["choices"][0]["delta"]
        assert "reasoning" not in delta
        assert delta["reasoning_content"] == "hmm..."
        assert delta["content"] == "result"

    def test_transform_stream_chunk_no_reasoning_no_change(self):
        provider = OllamaProvider()
        chunk = {
            "choices": [
                {
                    "delta": {
                        "content": "just text",
                    }
                }
            ]
        }
        transformed = provider._transform_stream_chunk(chunk)
        delta = transformed["choices"][0]["delta"]
        assert "reasoning_content" not in delta
        assert delta["content"] == "just text"

    def test_transform_stream_chunk_empty_choices(self):
        provider = OllamaProvider()
        chunk = {"choices": []}
        transformed = provider._transform_stream_chunk(chunk)
        assert transformed == {"choices": []}


class TestLMStudioProvider:
    def test_default_model_and_endpoint(self):
        provider = LMStudioProvider()
        assert provider.model == "local-model"
        assert "1234" in provider.endpoint
