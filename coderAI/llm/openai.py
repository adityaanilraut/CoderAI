"""OpenAI LLM provider implementation."""

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import tiktoken
from openai import AsyncOpenAI

from .base import LLMProvider

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY_BASE = 1.0  # seconds

# Cost per 1K tokens (approximate, as of early 2026)
MODEL_COSTS = {
    "gpt-5": {"input": 0.0025, "output": 0.01},
    "gpt-5-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "o1": {"input": 0.015, "output": 0.06},
    "o1-mini": {"input": 0.003, "output": 0.012},
    "o3-mini": {"input": 0.0011, "output": 0.0044},
}

# Models that do not support the 'temperature' parameter
# (only default value of 1 is allowed)
NO_TEMPERATURE_MODELS = {"gpt-5-mini", "o1", "o1-mini", "o3-mini"}


class OpenAIProvider(LLMProvider):
    """OpenAI LLM provider for GPT models."""

    # Supported models with their actual API names
    SUPPORTED_MODELS = {
        "gpt-5": "gpt-5",
        "gpt-5-mini": "gpt-5-mini",
        "gpt-4-turbo": "gpt-4-turbo",
        "gpt-4": "gpt-4",
        "gpt-3.5-turbo": "gpt-3.5-turbo",
        "o1": "o1",
        "o1-mini": "o1-mini",
        "o3-mini": "o3-mini",
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs):
        """Initialize OpenAI provider.

        Args:
            model: Model name (gpt-5, gpt-5-mini, gpt-4-turbo, etc.)
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

        # Some models don't support the temperature parameter
        if self.actual_model not in NO_TEMPERATURE_MODELS:
            params["temperature"] = kwargs.get("temperature", self.temperature)

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self.client.chat.completions.create(**params)
                result = response.model_dump()

                # Track usage for cost calculation
                usage = result.get("usage", {})
                self.total_input_tokens += usage.get("prompt_tokens", 0)
                self.total_output_tokens += usage.get("completion_tokens", 0)

                return result
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(
                        f"OpenAI API attempt {attempt + 1} failed: {e}. Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    raise RuntimeError(
                        f"OpenAI API error after {MAX_RETRIES} attempts: {str(last_error)}"
                    ) from last_error

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

        # Some models don't support the temperature parameter
        if self.actual_model not in NO_TEMPERATURE_MODELS:
            params["temperature"] = kwargs.get("temperature", self.temperature)

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        last_error = None
        for attempt in range(MAX_RETRIES):
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
                return  # Success, exit retry loop
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(
                        f"OpenAI stream attempt {attempt + 1} failed: {e}. Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    raise RuntimeError(
                        f"OpenAI API streaming error after {MAX_RETRIES} attempts: {str(last_error)}"
                    ) from last_error

    def count_tokens(self, text: str) -> int:
        """Count tokens using tiktoken.

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens
        """
        return len(self.tokenizer.encode(text))

    def count_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Count tokens in a list of messages.

        Args:
            messages: List of message dictionaries

        Returns:
            Total number of tokens
        """
        # Approximate token count for messages
        # OpenAI uses ~4 tokens per message for formatting
        num_tokens = 0
        for message in messages:
            num_tokens += 4  # Message formatting tokens
            for key, value in message.items():
                if isinstance(value, str):
                    num_tokens += self.count_tokens(value)
        num_tokens += 3  # Reply priming tokens
        return num_tokens

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
        return info
