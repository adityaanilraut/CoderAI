"""Groq LLM provider implementation."""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from groq import AsyncGroq

from .base import LLMProvider

logger = logging.getLogger(__name__)

# Approximate costs (often free or very low for Groq, but we can set 0 or estimates)
MODEL_COSTS = {
    "openai/gpt-oss-120b": {"input": 0.0, "output": 0.0},
    "openai/gpt-oss-20b": {"input": 0.0, "output": 0.0},
    "llama3-70b-8192": {"input": 0.00059, "output": 0.00079},
    "llama3-8b-8192": {"input": 0.00005, "output": 0.00008},
    "mixtral-8x7b-32768": {"input": 0.00024, "output": 0.00024},
    "gemma-7b-it": {"input": 0.00007, "output": 0.00007},
}

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

        self.actual_model = self.SUPPORTED_MODELS.get(model, model)
        self.client = AsyncGroq(api_key=api_key)

        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 4096)

        # Cost tracking
        self.total_input_tokens = 0
        self.total_output_tokens = 0

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

        response = await self.client.chat.completions.create(**params)
        result = response.model_dump()

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

        stream = await self.client.chat.completions.create(**params)
        async for chunk in stream:
            chunk_data = chunk.model_dump()
            
            # Groq streaming might include usage in specific chunks (like the last one)
            # Depending on Groq's exact streaming API for usage, we attempt to capture it:
            if getattr(chunk, "x_groq", None) and getattr(chunk.x_groq, "usage", None):
                usage = chunk.x_groq.usage.model_dump()
                self.total_input_tokens += usage.get("prompt_tokens", 0)
                self.total_output_tokens += usage.get("completion_tokens", 0)

            yield chunk_data

    def count_tokens(self, text: str) -> int:
        """Count tokens. Groq doesn't provide a direct tokenizer, using approx."""
        # Note: accurate tiktoken requires a specific model mapping; we use a rough char estimate here
        return len(text) // 4

    def get_cost(self) -> Dict[str, Any]:
        """Get current session cost estimate."""
        costs = MODEL_COSTS.get(self.actual_model, {"input": 0, "output": 0})
        input_cost = (self.total_input_tokens / 1000) * costs["input"]
        output_cost = (self.total_output_tokens / 1000) * costs["output"]

        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "input_cost": round(input_cost, 6),
            "output_cost": round(output_cost, 6),
            "total_cost": round(input_cost + output_cost, 6),
            "currency": "USD",
            "model": self.actual_model,
        }

    def get_model_info(self) -> Dict[str, Any]:
        info = super().get_model_info()
        info["cost"] = self.get_cost()
        info["total_input_tokens"] = self.total_input_tokens
        info["total_output_tokens"] = self.total_output_tokens
        info["total_tokens"] = self.total_input_tokens + self.total_output_tokens
        return info
