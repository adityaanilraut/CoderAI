"""Base class for OpenAI-compatible local LLM providers (Ollama, LM Studio, etc.)."""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import urljoin

import aiohttp

from coderAI.llm.base import (
    HTTP_CONNECT_TIMEOUT,
    HTTP_SOCK_READ_TIMEOUT,
    HTTP_TOTAL_TIMEOUT,
    LLMProvider,
)

logger = logging.getLogger(__name__)


class OpenAICompatibleLocalProvider(LLMProvider):
    """Base class for local LLM providers with OpenAI-compatible chat APIs.

    Shared implementation for ``chat()``, ``stream()``, ``count_tokens()``,
    ``get_cost()``, ``get_model_info()``, and ``supports_tools()``.
    Subclasses need only override the constructor defaults and any
    provider-specific streaming transformations (e.g. reasoning-content
    extraction for Ollama).
    """

    def __init__(self, model: str, endpoint: str, **kwargs):
        super().__init__(model, None, **kwargs)
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.endswith("/v1"):
            self.endpoint = urljoin(self.endpoint + "/", "v1")
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _build_payload(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        return payload

    def _get_url(self) -> str:
        return f"{self.endpoint}/chat/completions"

    def _track_usage(self, usage: Dict[str, Any]) -> None:
        self.total_input_tokens += usage.get("prompt_tokens", 0)
        self.total_output_tokens += usage.get("completion_tokens", 0)

    def _get_provider_label(self) -> str:
        """Human-readable provider name for error messages (e.g. 'LM Studio', 'Ollama')."""
        return self.__class__.__name__.replace("Provider", "")

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        url = self._get_url()
        payload = self._build_payload(messages, tools, **kwargs)
        async with self._get_session().post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(
                connect=HTTP_CONNECT_TIMEOUT,
                sock_read=HTTP_SOCK_READ_TIMEOUT,
                total=HTTP_TOTAL_TIMEOUT,
            ),
        ) as response:
            response.raise_for_status()
            try:
                result = await response.json()
            except Exception as e:
                label = self._get_provider_label()
                raise RuntimeError(f"{label} returned malformed JSON response: {e}") from e

            assert isinstance(result, dict)
            self._track_usage(result.get("usage", {}))
            return self._transform_chat_response(result)

    def _transform_chat_response(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Hook for subclasses to mutate the chat response before returning."""
        return result

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        url = self._get_url()
        payload = self._build_payload(messages, tools, stream=True, **kwargs)
        async with self._get_session().post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(
                connect=HTTP_CONNECT_TIMEOUT,
                sock_read=HTTP_SOCK_READ_TIMEOUT,
                total=HTTP_TOTAL_TIMEOUT,
            ),
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
                            chunk = json.loads(data)
                            self._track_usage(chunk.get("usage", {}))
                            transformed = self._transform_stream_chunk(chunk)
                            if transformed is not None:
                                yield transformed
                        except json.JSONDecodeError:
                            logger.debug("Failed to parse SSE data: %s", data)
                            continue

    def _transform_stream_chunk(self, chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Hook for subclasses to mutate each stream chunk before yielding."""
        return chunk

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
        return info
