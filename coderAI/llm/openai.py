"""OpenAI LLM provider implementation."""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import tiktoken
from openai import AsyncOpenAI

from .base import LLMProvider

logger = logging.getLogger(__name__)

# Cost per 1K tokens (approximate, as of early 2026)
MODEL_COSTS = {
    "gpt-5": {"input": 0.0025, "output": 0.01},
    "gpt-5-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-5-nano": {"input": 0.00005, "output": 0.0004},
    "o1": {"input": 0.015, "output": 0.06},
    "o1-mini": {"input": 0.003, "output": 0.012},
    "o3-mini": {"input": 0.0011, "output": 0.0044},
}



class OpenAIProvider(LLMProvider):
    """OpenAI LLM provider for GPT models."""

    # Supported models with their actual API names
    SUPPORTED_MODELS = {
        "gpt-5": "gpt-5",
        "gpt-5-mini": "gpt-5-mini",
        "gpt-5-nano": "gpt-5-nano",
        "o1": "o1",
        "o1-mini": "o1-mini",
        "o3-mini": "o3-mini",
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs):
        """Initialize OpenAI provider.

        Args:
            model: Model name (gpt-5, gpt-5-mini, gpt-5-nano, etc.)
            api_key: OpenAI API key
            **kwargs: Additional options (temperature, max_tokens, etc.)
        """
        super().__init__(model, api_key, **kwargs)

        # Use actual model name, or pass through if not in supported list
        self.actual_model = self.SUPPORTED_MODELS.get(model, model)

        # Initialize OpenAI client
        self.client = AsyncOpenAI(api_key=api_key)

        # Extract common parameters
        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 4096)
        self.reasoning_effort = kwargs.get("reasoning_effort", "medium")

        # Initialize tokenizer
        try:
            self.tokenizer = tiktoken.encoding_for_model(self.actual_model)
        except KeyError:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

        # Cost tracking
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat completion request to OpenAI with retry logic.

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
            "max_completion_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        # Handle logic for temperature and reasoning_effort
        is_gpt5 = self.actual_model in ("gpt-5", "gpt-5-mini", "gpt-5-nano")
        is_o_series = self.actual_model in ("o1", "o1-mini", "o3-mini")

        if is_gpt5 or is_o_series:
            # GPT-5 and o-series models should use reasoning_effort, not temperature.
            if self.reasoning_effort and self.reasoning_effort != "none":
                params["reasoning_effort"] = self.reasoning_effort
        else:
            params["temperature"] = kwargs.get("temperature", self.temperature)

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        try:
            response = await self.client.chat.completions.create(**params)
            result = response.model_dump()

            # Track usage for cost calculation
            usage = result.get("usage", {})
            self.total_input_tokens += usage.get("prompt_tokens", 0)
            self.total_output_tokens += usage.get("completion_tokens", 0)

            return result
        except Exception as e:
            msg = str(e)
            if "not a chat model" in msg and "v1/chat/completions" in msg:
                raise RuntimeError(
                    f"Model '{self.actual_model}' is not compatible with chat.completions "
                    "in this environment. Switch to gpt-5, gpt-5-mini, gpt-5-nano, "
                    "o1, o1-mini, or o3-mini."
                ) from e
            raise

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a streaming chat completion request to OpenAI with retry logic.

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
            "max_completion_tokens": kwargs.get("max_tokens", self.max_tokens),
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        # Handle logic for temperature and reasoning_effort
        is_gpt5 = self.actual_model in ("gpt-5", "gpt-5-mini", "gpt-5-nano")
        is_o_series = self.actual_model in ("o1", "o1-mini", "o3-mini")

        if is_gpt5 or is_o_series:
            # GPT-5 and o-series models should use reasoning_effort, not temperature.
            if self.reasoning_effort and self.reasoning_effort != "none":
                params["reasoning_effort"] = self.reasoning_effort
        else:
            params["temperature"] = kwargs.get("temperature", self.temperature)

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        try:
            stream = await self.client.chat.completions.create(**params)
            async for chunk in stream:
                chunk_data = chunk.model_dump()
                # Track streaming usage (final chunk contains usage info)
                usage = chunk_data.get("usage")
                if usage:
                    self.total_input_tokens += usage.get("prompt_tokens", 0)
                    self.total_output_tokens += usage.get("completion_tokens", 0)
                yield chunk_data
        except Exception as e:
            msg = str(e)
            if "not a chat model" in msg and "v1/chat/completions" in msg:
                raise RuntimeError(
                    f"Model '{self.actual_model}' is not compatible with chat.completions "
                    "in this environment. Switch to gpt-5, gpt-5-mini, gpt-5-nano, "
                    "o1, o1-mini, or o3-mini."
                ) from e
            raise

    def count_tokens(self, text: str) -> int:
        """Count tokens using tiktoken.

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens
        """
        return len(self.tokenizer.encode(text))

    def get_cost(self) -> Dict[str, Any]:
        """Get current session cost estimate.

        Returns:
            Dictionary with cost breakdown
        """
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
        """Get information about the current model."""
        info = super().get_model_info()
        info["cost"] = self.get_cost()
        info["total_input_tokens"] = self.total_input_tokens
        info["total_output_tokens"] = self.total_output_tokens
        info["total_tokens"] = self.total_input_tokens + self.total_output_tokens
        return info
