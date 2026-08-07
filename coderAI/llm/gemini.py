"""Gemini LLM provider implementation."""

import logging
from typing import Any, Optional
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from coderAI.llm.base import HTTP_TOTAL_TIMEOUT
from coderAI.llm.cloud_base import OpenAICompatibleCloudProvider
from coderAI.system.error_policy import _try_extract_response_body
from coderAI.system.redaction import redact_secrets, redact_text

logger = logging.getLogger(__name__)


class GeminiProvider(OpenAICompatibleCloudProvider):
    """Gemini LLM provider using the OpenAI-compatible API."""

    PROVIDER_LABEL = "Gemini"

    SUPPORTED_MODELS = {
        "gemini-3.5-flash": "gemini-3.5-flash",
        "gemini-3.6-flash": "gemini-3.6-flash",
        "gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
    }
    MODEL_CONTEXT_WINDOWS = {
        "gemini-3.5-flash": 1_048_576,
        "gemini-3.6-flash": 1_048_576,
        "gemini-3.1-flash-lite": 1_048_576,
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        super().__init__(model, api_key, **kwargs)

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            timeout=HTTP_TOTAL_TIMEOUT,
        )

    def _resolve_model(self, model: str) -> str:
        from coderAI.llm.registry import resolve_alias

        return resolve_alias(model).lower()

    def _handle_api_error(self, exc: Exception, *, streaming: bool) -> None:
        verb = "streaming error" if streaming else "error"
        logger.error("Gemini API %s: %s", verb, redact_text(str(exc)))
        body = _try_extract_response_body(exc)
        if body is not None:
            logger.error("Gemini API %s body: %s", verb, redact_secrets(body))
        super()._handle_api_error(exc, streaming=streaming)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        # Gemini's OpenAI-compatible endpoint may finish a stream without ever
        # reporting usage; accumulate content so we can estimate in that case.
        accumulated_content = ""
        had_usage = False
        async for chunk_data in super().stream(messages, tools, **kwargs):
            if chunk_data.get("usage"):
                had_usage = True

            choices = chunk_data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                accumulated_content += delta.get("content", "") or ""
                accumulated_content += delta.get("reasoning_content", "") or ""

            yield chunk_data

        if not had_usage:
            est_out = self.count_tokens(accumulated_content) if accumulated_content else 0
            est_in = 0
            if not self.total_input_tokens:
                input_text = " ".join(
                    m.get("content", "") or ""
                    for m in messages
                    if isinstance(m.get("content"), str)
                )
                est_in = self.count_tokens(input_text)
            self.total_output_tokens += est_out
            self.total_input_tokens += est_in
            # No usage chunk arrived from the API, so surface the estimate to
            # the streaming handler as a trailing usage-only chunk.
            if est_in or est_out:
                yield {
                    "choices": [],
                    "usage": {"prompt_tokens": est_in, "completion_tokens": est_out},
                }

    def clean_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Round-trip reasoning and render tool images as multimodal content."""
        return self._render_tool_images_openai(messages)
