"""Groq LLM provider implementation."""

from typing import Any, AsyncIterator, Dict, List, Optional

from groq import AsyncGroq

from coderAI.llm.base import LLMProvider
from coderAI.llm.base import _retry_async as _retry


class GroqProvider(LLMProvider):
    """Groq LLM provider."""

    SUPPORTED_MODELS = {
        "openai/gpt-oss-120b": "openai/gpt-oss-120b",
        "openai/gpt-oss-20b": "openai/gpt-oss-20b",
        "llama3-70b-8192": "llama3-70b-8192",
        "llama3-8b-8192": "llama3-8b-8192",
        "mixtral-8x7b-32768": "mixtral-8x7b-32768",
        "gemma-7b-it": "gemma-7b-it",
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs):
        """Initialize Groq provider.

        Args:
            model: Model name
            api_key: Groq API key
            **kwargs: Additional options (temperature, max_tokens, etc.)
        """
        super().__init__(model, api_key, **kwargs)

        if not api_key:
            raise ValueError("Groq API key is required")

        self.actual_model = self.SUPPORTED_MODELS.get(model, model)
        self.client = AsyncGroq(api_key=api_key)

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat completion request to Groq.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Returns:
            Response dictionary
        """
        params = {
            "model": self.actual_model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        try:

            async def _call():
                return await self.client.chat.completions.create(**params)

            response = await _retry(_call, description="Groq chat", max_retries=3)
        except Exception as e:
            raise RuntimeError(f"Groq API error: {e}") from e
        result = response.model_dump()
        assert isinstance(result, dict)

        usage = result.get("usage", {})
        self.total_input_tokens += usage.get("prompt_tokens", 0)
        self.total_output_tokens += usage.get("completion_tokens", 0)

        return result

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a streaming chat completion request to Groq.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Yields:
            Response chunks
        """
        params = {
            "model": self.actual_model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "stream": True,
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        try:

            async def _create_stream():
                return await self.client.chat.completions.create(**params)

            stream = await _retry(_create_stream, description="Groq stream", max_retries=3)
        except Exception as e:
            raise RuntimeError(f"Groq API streaming error: {e}") from e
        async for chunk in stream:
            chunk_data = chunk.model_dump()

            # Groq streaming may include usage in the chunk data dict.
            usage = chunk_data.get("x_groq", {}).get("usage")
            if usage:
                self.total_input_tokens += usage.get("prompt_tokens", 0)
                self.total_output_tokens += usage.get("completion_tokens", 0)

            yield chunk_data
