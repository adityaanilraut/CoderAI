"""Groq LLM provider implementation."""

from typing import Any, Dict, Optional

from groq import AsyncGroq

from coderAI.llm.base import HTTP_TOTAL_TIMEOUT
from coderAI.llm.cloud_base import OpenAICompatibleCloudProvider


class GroqProvider(OpenAICompatibleCloudProvider):
    """Groq LLM provider."""

    PROVIDER_LABEL = "Groq"

    SUPPORTED_MODELS = {
        "openai/gpt-oss-120b": "openai/gpt-oss-120b",
        "openai/gpt-oss-20b": "openai/gpt-oss-20b",
        "llama3-70b-8192": "llama3-70b-8192",
        "llama3-8b-8192": "llama3-8b-8192",
        "mixtral-8x7b-32768": "mixtral-8x7b-32768",
        "gemma-7b-it": "gemma-7b-it",
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        super().__init__(model, api_key, **kwargs)
        self.client = AsyncGroq(api_key=api_key, timeout=HTTP_TOTAL_TIMEOUT)

    def _extract_stream_usage(self, chunk_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Groq reports streaming usage under ``x_groq`` rather than top-level.
        usage = chunk_data.get("x_groq", {}).get("usage")
        if usage:
            # Surface it at the top level so the streaming handler (which
            # only reads ``chunk["usage"]``) can attribute per-call usage.
            chunk_data["usage"] = usage
        return usage if isinstance(usage, dict) else None
