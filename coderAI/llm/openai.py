"""OpenAI LLM provider implementation."""

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

import tiktoken
from openai import AsyncOpenAI

from .base import LLMProvider


class OpenAIProvider(LLMProvider):
    """OpenAI LLM provider for GPT-5 models."""

    # Model name mappings
    MODEL_MAPPINGS = {
        "gpt-5": "gpt-4",  # Will be updated when GPT-5 is released
        "gpt-5-mini": "gpt-4-turbo-preview",
        "gpt-5-nano": "gpt-3.5-turbo",
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs):
        """Initialize OpenAI provider.

        Args:
            model: Model name (gpt-5, gpt-5-mini, gpt-5-nano)
            api_key: OpenAI API key
            **kwargs: Additional options (temperature, max_tokens, etc.)
        """
        super().__init__(model, api_key, **kwargs)

        # Map model names to actual OpenAI models
        self.actual_model = self.MODEL_MAPPINGS.get(model, model)

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
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        try:
            response = await self.client.chat.completions.create(**params)
            return response.model_dump()
        except Exception as e:
            raise RuntimeError(f"OpenAI API error: {str(e)}") from e

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
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "stream": True,
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        try:
            stream = await self.client.chat.completions.create(**params)
            async for chunk in stream:
                yield chunk.model_dump()
        except Exception as e:
            raise RuntimeError(f"OpenAI API error: {str(e)}") from e

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
        # OpenAI uses ~3 tokens per message for formatting
        num_tokens = 0
        for message in messages:
            num_tokens += 3  # Message formatting tokens
            for key, value in message.items():
                if isinstance(value, str):
                    num_tokens += self.count_tokens(value)
        num_tokens += 3  # Reply priming tokens
        return num_tokens

