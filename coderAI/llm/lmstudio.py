"""LM Studio local LLM provider implementation."""

import logging

from coderAI.llm.local_base import OpenAICompatibleLocalProvider

logger = logging.getLogger(__name__)


class LMStudioProvider(OpenAICompatibleLocalProvider):
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
        super().__init__(model=model, endpoint=endpoint, **kwargs)
