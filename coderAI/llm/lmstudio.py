"""LM Studio local LLM provider implementation."""

from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from .base import LLMProvider


class LMStudioProvider(LLMProvider):
    """LM Studio local LLM provider using OpenAI-compatible API."""

    def __init__(
        self, model: str = "local-model", endpoint: str = "http://localhost:1234/v1", **kwargs
    ):
        """Initialize LM Studio provider.

        Args:
            model: Model name (not strictly required for LM Studio)
            endpoint: LM Studio API endpoint (default: http://localhost:1234/v1)
            **kwargs: Additional options
        """
        super().__init__(model, None, **kwargs)
        self.endpoint = endpoint.rstrip("/")
        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 4096)

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat completion request to LM Studio.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Returns:
            Response dictionary
        """
        url = f"{self.endpoint}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    response.raise_for_status()
                    return await response.json()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"LM Studio API error: {str(e)}") from e
        except Exception as e:
            raise RuntimeError(f"Unexpected error: {str(e)}") from e

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a streaming chat completion request to LM Studio.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Yields:
            Response chunks
        """
        url = f"{self.endpoint}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "stream": True,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.content:
                        line = line.decode("utf-8").strip()
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                import json

                                yield json.loads(data)
                            except json.JSONDecodeError:
                                continue
        except aiohttp.ClientError as e:
            raise RuntimeError(f"LM Studio API error: {str(e)}") from e
        except Exception as e:
            raise RuntimeError(f"Unexpected error: {str(e)}") from e

    def count_tokens(self, text: str) -> int:
        """Approximate token count for local models.

        Args:
            text: Text to count tokens for

        Returns:
            Approximate number of tokens
        """
        # Rough approximation: 1 token ≈ 4 characters
        return len(text) // 4

    def supports_tools(self) -> bool:
        """Check if tools are supported.

        Returns:
            True - LM Studio with compatible models supports tool calling
        """
        # LM Studio supports tool calling with compatible models like Qwen
        return True

