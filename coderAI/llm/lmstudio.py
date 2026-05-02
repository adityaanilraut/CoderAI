"""LM Studio local LLM provider implementation."""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from .base import LLMProvider

logger = logging.getLogger(__name__)


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
        if not self.endpoint.endswith("/v1"):
            self.endpoint = f"{self.endpoint}/v1"
        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 8192)

        # Token tracking for session info
        self.total_input_tokens = 0
        self.total_output_tokens = 0

        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a persistent HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Send a chat completion request to LM Studio."""
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

        async with self._get_session().post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=120)
        ) as response:
            response.raise_for_status()
            try:
                result = await response.json()
            except Exception as e:
                raise RuntimeError(f"LM Studio returned malformed JSON response: {e}") from e

            # Track usage
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
        """Send a streaming chat completion request to LM Studio."""
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

        async with self._get_session().post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=120)
        ) as response:
            response.raise_for_status()
            buffer = ""
            async for raw_chunk in response.content:
                buffer += raw_chunk.decode("utf-8")
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
                            yield json.loads(data)
                        except json.JSONDecodeError:
                            logger.debug(f"Failed to parse SSE data: {data}")
                            continue

    def count_tokens(self, text: str) -> int:
        """Approximate token count for local models."""
        return len(text) // 4

    def supports_tools(self) -> bool:
        return True

    def get_cost(self) -> Dict[str, Any]:
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
        info = super().get_model_info()
        info["endpoint"] = self.endpoint
        info["cost"] = self.get_cost()
        info["total_input_tokens"] = self.total_input_tokens
        info["total_output_tokens"] = self.total_output_tokens
        info["total_tokens"] = self.total_input_tokens + self.total_output_tokens
        return info
