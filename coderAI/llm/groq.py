"""Groq LLM provider implementation."""

from typing import Any, Optional

from coderAI.llm.base import HTTP_TOTAL_TIMEOUT
from coderAI.llm.cloud_base import OpenAICompatibleCloudProvider


class GroqProvider(OpenAICompatibleCloudProvider):
    """Groq LLM provider."""

    PROVIDER_ID = "groq"
    CONFIG_API_KEY = "groq_api_key"
    MODEL_PREFIX = "groq"
    PROVIDER_LABEL = "Groq"

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        super().__init__(model, api_key, **kwargs)
        try:
            from groq import AsyncGroq
        except ImportError as e:
            raise ImportError(
                "groq is not installed. Install with: pip install coderai-agent[groq]"
            ) from e
        self.client = AsyncGroq(api_key=api_key, timeout=HTTP_TOTAL_TIMEOUT)

    def _extract_stream_usage(self, chunk_data: dict[str, Any]) -> Optional[dict[str, Any]]:
        # Groq reports streaming usage under ``x_groq`` rather than top-level.
        usage = chunk_data.get("x_groq", {}).get("usage")
        if usage:
            # Surface it at the top level so the streaming handler (which
            # only reads ``chunk["usage"]``) can attribute per-call usage.
            chunk_data["usage"] = usage
        return usage if isinstance(usage, dict) else None
