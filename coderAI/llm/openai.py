"""OpenAI LLM provider implementation."""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import openai
import tiktoken
from openai import AsyncOpenAI

from .base import LLMProvider
from coderAI.cost import CostTracker

logger = logging.getLogger(__name__)



class OpenAIProvider(LLMProvider):
    """OpenAI LLM provider for GPT models."""

    # Supported models with their actual API names
    SUPPORTED_MODELS = {
        "gpt-5.4": "gpt-5.4",
        "gpt-5.4-mini": "gpt-5.4-mini",
        "gpt-5.4-nano": "gpt-5.4-nano",
        "o1": "o1",
        "o1-mini": "o1-mini",
        "o1-pro": "o1-pro",
        "o3-mini": "o3-mini",
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs):
        """Initialize OpenAI provider.

        Args:
            model: Model name (gpt-5.4, gpt-5.4-mini, gpt-5.4-nano, etc.)
            api_key: OpenAI API key
            **kwargs: Additional options (temperature, max_tokens, etc.)
        """
        super().__init__(model, api_key, **kwargs)

        if not api_key:
            raise ValueError("OpenAI API key is required")

        self.actual_model = self.SUPPORTED_MODELS.get(model, model)
        self.client = AsyncOpenAI(api_key=api_key)

        # Extract common parameters
        self.temperature = kwargs.get("temperature", 1)
        self.max_tokens = kwargs.get("max_tokens", 8192)
        self.reasoning_effort = kwargs.get("reasoning_effort", "medium")

        # Initialize tokenizer
        try:
            self.tokenizer = tiktoken.encoding_for_model(self.actual_model)
        except KeyError:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

        # Cost tracking
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # Models that don't support temperature (only accept default=1)
    _NO_TEMPERATURE_MODELS_PREFIX = ("gpt-5",)
    _NO_TEMPERATURE_MODELS_EXACT = {"o1", "o1-mini", "o1-pro", "o3-mini"}

    # Models that cannot accept reasoning_effort with function tools in /v1/chat/completions
    _NO_REASONING_EFFORT_MODELS = {"gpt-5.4-nano", "gpt-5.4-mini"}

    @property
    def _rejects_temperature(self) -> bool:
        """Whether this model rejects temperature (only accepts default=1).

        gpt-5.x models (and o-series) only support temperature=1 (the default),
        so we omit the parameter entirely.
        """
        return (
            self.actual_model.startswith(self._NO_TEMPERATURE_MODELS_PREFIX)
            or self.actual_model in self._NO_TEMPERATURE_MODELS_EXACT
        )

    @property
    def _supports_reasoning_effort(self) -> bool:
        """Whether this model accepts the reasoning_effort parameter.

        gpt-5.4-nano does not support reasoning_effort with function tools
        in /v1/chat/completions, so we omit it for that model.
        """
        return (
            self._rejects_temperature
            and self.actual_model not in self._NO_REASONING_EFFORT_MODELS
        )

    def _apply_temp_and_reasoning(self, params: Dict[str, Any], **kwargs) -> None:
        if self._rejects_temperature:
            if self._supports_reasoning_effort and self.reasoning_effort and self.reasoning_effort != "none":
                params["reasoning_effort"] = self.reasoning_effort
        else:
            params["temperature"] = kwargs.get("temperature", self.temperature)

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat completion request to OpenAI.

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

        self._apply_temp_and_reasoning(params, **kwargs)

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
        except openai.APIError as e:
            msg = str(e)
            if "not a chat model" in msg and "v1/chat/completions" in msg:
                raise RuntimeError(
                    f"Model '{self.actual_model}' is not compatible with chat.completions "
                    "in this environment. Switch to gpt-5.4, gpt-5.4-mini, gpt-5.4-nano, "
                    "o1, o1-mini, or o3-mini."
                ) from e
            raise

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a streaming chat completion request to OpenAI.

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

        self._apply_temp_and_reasoning(params, **kwargs)

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
        except openai.APIError as e:
            msg = str(e)
            if "not a chat model" in msg and "v1/chat/completions" in msg:
                raise RuntimeError(
                    f"Model '{self.actual_model}' is not compatible with chat.completions "
                    "in this environment. Switch to gpt-5.4, gpt-5.4-mini, gpt-5.4-nano, "
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
        try:
            return len(self.tokenizer.encode(text))
        except Exception:
            return len(text) // 4

    def get_cost(self) -> Dict[str, Any]:
        """Get current session cost estimate.

        Returns:
            Dictionary with cost breakdown
        """
        pricing = CostTracker.get_model_pricing(self.actual_model)
        input_cost = (self.total_input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.total_output_tokens / 1_000_000) * pricing["output"]

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

    async def close(self) -> None:
        await self.client.close()
