"""Gemini LLM provider implementation."""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from coderAI.llm.base import LLMProvider
from coderAI.llm.base import _retry_async as _retry
from coderAI.system.error_policy import _try_extract_response_body
from coderAI.system.redaction import sanitize_dict as _sanitize_dict
from coderAI.system.safeguards import sanitize_for_log

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """Gemini LLM provider using the OpenAI-compatible API."""

    SUPPORTED_MODELS = {
        "gemini-3.5-flash": "gemini-3.5-flash",
        "gemini-3.1-pro": "gemini-3.1-pro",
        "gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
        "gemini-2.5-flash": "gemini-2.5-flash",
        "gemini-2.5-pro": "gemini-2.5-pro",
        "gemini-2.0-flash": "gemini-2.0-flash",
        "gemini-2.0-pro": "gemini-2.0-pro",
        "gemini-1.5-flash": "gemini-1.5-flash",
        "gemini-1.5-pro": "gemini-1.5-pro",
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        """Initialize Gemini provider.

        Args:
            model: Model name
            api_key: Gemini API key
            **kwargs: Additional options (temperature, max_tokens, etc.)
        """
        super().__init__(model, api_key, **kwargs)

        if not api_key:
            raise ValueError("Gemini API key is required")

        self.actual_model = self.SUPPORTED_MODELS.get(model.lower(), model.lower())

        # Gemini uses OpenAI-compatible API
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

    def _build_request_params(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "model": self.actual_model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        if stream:
            params["stream"] = True

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        return params

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a chat completion request to Gemini.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Returns:
            Response dictionary
        """
        params = self._build_request_params(messages, tools, **kwargs)

        try:

            async def _call() -> Any:
                return await self.client.chat.completions.create(**params)

            response = await _retry(_call, description="Gemini chat", max_retries=3)
        except Exception as e:
            logger.error("Gemini API error: %s", e)
            body = _try_extract_response_body(e)
            if body is not None:
                logger.error("Gemini API error body: %s", _sanitize_dict(body))
            raise RuntimeError(f"Gemini API error: {sanitize_for_log(str(e))}") from e
        result = response.model_dump()
        assert isinstance(result, dict)

        usage = result.get("usage") or {}
        self.total_input_tokens += usage.get("prompt_tokens", 0)
        self.total_output_tokens += usage.get("completion_tokens", 0)

        return result

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a streaming chat completion request to Gemini.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Yields:
            Response chunks
        """
        params = self._build_request_params(messages, tools, stream=True, **kwargs)

        try:

            async def _create_stream() -> Any:
                return await self.client.chat.completions.create(**params)

            stream = await _retry(_create_stream, description="Gemini stream", max_retries=3)
        except Exception as e:
            logger.error("Gemini API streaming error: %s", e)
            body = _try_extract_response_body(e)
            if body is not None:
                logger.error("Gemini API streaming error body: %s", _sanitize_dict(body))
            raise RuntimeError(f"Gemini API streaming error: {sanitize_for_log(str(e))}") from e

        accumulated_content = ""
        had_usage = False
        async for chunk in stream:
            chunk_data = chunk.model_dump()

            usage = chunk_data.get("usage")
            if usage:
                had_usage = True
                self.total_input_tokens += usage.get("prompt_tokens", 0)
                self.total_output_tokens += usage.get("completion_tokens", 0)

            choices = chunk_data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                accumulated_content += delta.get("content", "") or ""
                accumulated_content += delta.get("reasoning_content", "") or ""

            yield chunk_data

        if not had_usage:
            if accumulated_content:
                self.total_output_tokens += self.count_tokens(accumulated_content)
            if not self.total_input_tokens:
                input_text = " ".join(
                    m.get("content", "") or ""
                    for m in messages
                    if isinstance(m.get("content"), str)
                )
                self.total_input_tokens += self.count_tokens(input_text)

    def clean_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Gemini supports passing reasoning_content back in the assistant role."""
        return messages
