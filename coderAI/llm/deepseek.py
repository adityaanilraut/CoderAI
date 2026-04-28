"""DeepSeek LLM provider implementation."""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from .base import LLMProvider
from coderAI.cost import CostTracker

logger = logging.getLogger(__name__)

class DeepSeekProvider(LLMProvider):
    """DeepSeek LLM provider."""

    SUPPORTED_MODELS = {
        "deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-chat": "deepseek-chat",
        "deepseek-reasoner": "deepseek-reasoner",
        "deepseek-v3": "deepseek-chat",
        "deepseek-v3.2": "deepseek-chat",  # Legacy alias retained for compatibility
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
        self.reasoning_effort = kwargs.get("reasoning_effort", "medium")

        # Cost tracking
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    @property
    def _uses_v4_family(self) -> bool:
        return self.actual_model in {"deepseek-v4-flash", "deepseek-v4-pro"}

    def _build_request_params(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "model": self.actual_model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        if stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}

        if tools and self.actual_model != "deepseek-reasoner":
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        # DeepSeek V4 defaults thinking mode to enabled. This agent loop does
        # not yet round-trip reasoning_content across tool turns, so keep the
        # new V4 IDs in non-thinking mode by default for compatibility.
        if self._uses_v4_family:
            params["extra_body"] = {"thinking": {"type": "disabled"}}

        return params

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
        params = self._build_request_params(messages, tools, **kwargs)

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
        params = self._build_request_params(messages, tools, stream=True, **kwargs)

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

    def get_cost(self) -> Dict[str, Any]:
        """Get current session cost estimate."""
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
        info = super().get_model_info()
        info["cost"] = self.get_cost()
        info["total_input_tokens"] = self.total_input_tokens
        info["total_output_tokens"] = self.total_output_tokens
        info["total_tokens"] = self.total_input_tokens + self.total_output_tokens
        return info
