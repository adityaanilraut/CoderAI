"""Ollama local LLM provider implementation."""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from .base import LLMProvider

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """Ollama local LLM provider using OpenAI-compatible API."""

    def __init__(
        self, model: str = "llama3", endpoint: str = "http://localhost:11434/v1", **kwargs
    ):
        """Initialize Ollama provider.

        Args:
            model: Model name
            endpoint: Ollama API endpoint (default: http://localhost:11434/v1)
            **kwargs: Additional options
        """
        super().__init__(model, None, **kwargs)
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.endswith("/v1"):
            self.endpoint = f"{self.endpoint}/v1"
        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 4096)

        # Token tracking for session info
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat completion request to Ollama with retry logic.

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

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                response.raise_for_status()
                result = await response.json()

                # Track usage
                usage = result.get("usage", {})
                self.total_input_tokens += usage.get("prompt_tokens", 0)
                self.total_output_tokens += usage.get("completion_tokens", 0)

                # If the model returned reasoning content, inject it into
                # the response as <think> tags so the streaming handler
                # can display it consistently.
                choices = result.get("choices", [])
                if choices:
                    message = choices[0].get("message", {})
                    reasoning = message.pop("reasoning", "")
                    if reasoning:
                        content = message.get("content", "")
                        message["content"] = f"<think>\n{reasoning}\n</think>\n\n{content}"

                return result

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a streaming chat completion request to Ollama with retry logic.

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

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                response.raise_for_status()
                # Buffer for handling multi-line SSE events
                buffer = ""
                async for raw_chunk in response.content:
                    buffer += raw_chunk.decode("utf-8")
                    # Process complete lines from the buffer
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                return
                            try:
                                chunk = json.loads(data)
                                # Transform reasoning into standard reasoning_content delta
                                choices = chunk.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    reasoning = delta.pop("reasoning", None)
                                    if reasoning:
                                        delta["reasoning_content"] = reasoning
                                yield chunk
                            except json.JSONDecodeError:
                                logger.debug(f"Failed to parse SSE data: {data}")
                                continue

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
            True - Ollama models supporting tools can be used. Just setting True allows its use.
        """
        return True

    def get_cost(self) -> Dict[str, Any]:
        """Get token usage (no cost for local models).

        Returns:
            Dictionary with usage info
        """
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "input_cost": 0,
            "output_cost": 0,
            "total_cost": 0,
            "currency": "USD",
            "model": self.model,
            "note": "Local model — no API cost",
        }

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        info = super().get_model_info()
        info["endpoint"] = self.endpoint
        info["cost"] = self.get_cost()
        info["total_input_tokens"] = self.total_input_tokens
        info["total_output_tokens"] = self.total_output_tokens
        info["total_tokens"] = self.total_input_tokens + self.total_output_tokens
        return info
