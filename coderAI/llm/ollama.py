"""Ollama local LLM provider implementation."""

import logging
from typing import Any, Dict, Optional

from coderAI.llm.local_base import OpenAICompatibleLocalProvider

logger = logging.getLogger(__name__)


class OllamaProvider(OpenAICompatibleLocalProvider):
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
        super().__init__(model=model, endpoint=endpoint, **kwargs)

    def _transform_chat_response(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Inject reasoning content as <think> tags for Ollama models."""
        choices = result.get("choices", [])
        if choices:
            message = dict(choices[0].get("message", {}))
            reasoning = message.pop("reasoning", "")
            if reasoning:
                content = message.get("content") or ""
                message["content"] = f"<think>\n{reasoning}\n</think>\n\n{content}"
            choices[0]["message"] = message
        return result

    def _transform_stream_chunk(self, chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Move Ollama reasoning delta to reasoning_content for streaming handler."""
        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            reasoning = delta.pop("reasoning", None)
            if reasoning:
                delta["reasoning_content"] = reasoning
        return chunk
