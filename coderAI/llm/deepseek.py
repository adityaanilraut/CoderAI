"""DeepSeek LLM provider implementation."""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from .base import LLMProvider

logger = logging.getLogger(__name__)

# Approximate costs for DeepSeek API
MODEL_COSTS = {
    "deepseek-chat": {"input": 0.00014, "output": 0.00028}, # DeepSeek-V3
    "deepseek-reasoner": {"input": 0.00055, "output": 0.00219}, # DeepSeek-R1
}

class DeepSeekProvider(LLMProvider):
    """DeepSeek LLM provider."""

    SUPPORTED_MODELS = {
        "deepseek-chat": "deepseek-chat",
        "deepseek-reasoner": "deepseek-reasoner",
        "deepseek-v3": "deepseek-chat",
        "deepseek-v3.2": "deepseek-chat", # Alias for the latest v3.2/v3 chat model
        "deepseek-r1": "deepseek-reasoner",
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs):
        """Initialize DeepSeek provider.

        Args:
            model: Model name
            api_key: DeepSeek API key
            **kwargs: Additional options (temperature, max_tokens, etc.)
        """
        super().__init__(model, api_key, **kwargs)

        self.actual_model = self.SUPPORTED_MODELS.get(model.lower(), model)
        
        # DeepSeek uses OpenAI-compatible API
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )

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
        """Send a chat completion request to DeepSeek.

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

        if tools and self.actual_model != "deepseek-reasoner":
            # Reasoner model might not support tools yet, but chat does
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
        """Send a streaming chat completion request to DeepSeek.

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
            "stream_options": {"include_usage": True},
        }

        if tools and self.actual_model != "deepseek-reasoner":
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        stream = await self.client.chat.completions.create(**params)
        async for chunk in stream:
            chunk_data = chunk.model_dump()
            
            # OpenAI clients return usage in chunks when stream_options={"include_usage": True} is passed
            usage = chunk_data.get("usage")
            if usage:
                self.total_input_tokens += usage.get("prompt_tokens", 0)
                # Ensure we account for completion / reasoning tokens appropriately if provided
                completion_tokens = usage.get("completion_tokens", 0)
                self.total_output_tokens += completion_tokens

            yield chunk_data

    def count_tokens(self, text: str) -> int:
        """Count tokens. Using a rough estimate of 4 chars per token."""
        return len(text) // 4

    def count_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        num_tokens = 0
        for message in messages:
            num_tokens += 4
            for key, value in message.items():
                if isinstance(value, str):
                    num_tokens += self.count_tokens(value)
        num_tokens += 3
        return num_tokens

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
