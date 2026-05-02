"""Base LLM provider interface."""

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, List, Optional

# Shared reasoning-effort → budget-tokens mapping used by Anthropic and DeepSeek.
REASONING_BUDGET_MAP = {"high": 16384, "medium": 8192, "low": 2048}


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs):
        """Initialize the LLM provider.

        Args:
            model: Model name to use
            api_key: API key for authentication
            **kwargs: Additional provider-specific options
        """
        self.model = model
        self.api_key = api_key
        self.options = kwargs

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat completion request.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Returns:
            Response dictionary with 'choices' containing the completion
        """
        pass

    @abstractmethod
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a streaming chat completion request.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Yields:
            Response chunks as they arrive
        """
        pass

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in text.

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens
        """
        pass

    def supports_tools(self) -> bool:
        """Check if the provider supports tool calling.

        Returns:
            True if tools are supported
        """
        return True

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        return {
            "provider": self.__class__.__name__,
            "model": self.model,
            "temperature": getattr(self, "temperature", 1.0),
        }

    async def close(self) -> None:
        """Clean up resources (sessions, connections, etc.)."""

    def set_cumulative_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Realign cumulative usage counters after a model switch or reset.

        Providers track different subsets of these (Anthropic exposes cache
        counters, others don't), so each attribute is updated only when the
        provider already defines it.
        """
        if hasattr(self, "total_input_tokens"):
            self.total_input_tokens = max(0, int(input_tokens or 0))
        if hasattr(self, "total_output_tokens"):
            self.total_output_tokens = max(0, int(output_tokens or 0))
        if hasattr(self, "total_cache_creation_tokens"):
            self.total_cache_creation_tokens = max(0, int(cache_creation_tokens or 0))
        if hasattr(self, "total_cache_read_tokens"):
            self.total_cache_read_tokens = max(0, int(cache_read_tokens or 0))

