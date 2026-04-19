"""Base LLM provider interface."""

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, List, Optional


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
        """Get information about the current model.

        Returns:
            Dictionary with model information
        """
        return {
            "model": self.model,
            "supports_tools": self.supports_tools(),
            "provider": self.__class__.__name__,
        }

