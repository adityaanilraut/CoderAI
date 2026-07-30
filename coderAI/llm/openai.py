"""OpenAI LLM provider implementation."""

from typing import Any, Optional

import openai
import tiktoken
from openai import AsyncOpenAI

from coderAI.llm.base import HTTP_TOTAL_TIMEOUT
from coderAI.llm.cloud_base import OpenAICompatibleCloudProvider


class OpenAIProvider(OpenAICompatibleCloudProvider):
    """OpenAI LLM provider for GPT models."""

    PROVIDER_LABEL = "OpenAI"
    STREAM_INCLUDES_USAGE = True

    # Supported models with their actual API names
    SUPPORTED_MODELS = {
        "gpt-5.6": "gpt-5.6-sol",
        "gpt-5.6-sol": "gpt-5.6-sol",
        "gpt-5.4": "gpt-5.4",
        "gpt-5.4-mini": "gpt-5.4-mini",
        "gpt-5.4-nano": "gpt-5.4-nano",
        "o1": "o1",
        "o1-mini": "o1-mini",
        "o1-pro": "o1-pro",
        "o3-mini": "o3-mini",
    }
    MODEL_CONTEXT_WINDOWS = {
        "gpt-5.6-sol": 1_050_000,
        "gpt-5.4": 1_050_000,
        "gpt-5.4-mini": 400_000,
        "gpt-5.4-nano": 400_000,
        "o1": 200_000,
        "o1-mini": 128_000,
        "o1-pro": 200_000,
        "o3-mini": 200_000,
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        super().__init__(model, api_key, **kwargs)

        self.client = AsyncOpenAI(api_key=api_key, timeout=HTTP_TOTAL_TIMEOUT)

        # OpenAI default temperature is 1.0, not 0.7
        if "temperature" not in kwargs:
            self.temperature = 1.0

        # Initialize tokenizer
        try:
            self.tokenizer = tiktoken.encoding_for_model(self.actual_model)
        except KeyError:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def _check_chat_model_compat(self, exc: Exception) -> None:
        """Raise a helpful RuntimeError when the model is not a chat model."""
        msg = str(exc)
        if "not a chat model" in msg and "v1/chat/completions" in msg:
            raise RuntimeError(
                f"Model '{self.actual_model}' is not compatible with chat.completions "
                "in this environment. Switch to gpt-5.6, gpt-5.4, gpt-5.4-mini, gpt-5.4-nano, "
                "o1, o1-mini, or o3-mini."
            ) from exc

    # Models that don't support temperature (only accept default=1)
    _NO_TEMPERATURE_MODELS_PREFIX = ("gpt-5",)
    _NO_TEMPERATURE_MODELS_EXACT = {"o1", "o1-mini", "o1-pro", "o3-mini"}

    # Models that cannot accept reasoning_effort with function tools in /v1/chat/completions
    _NO_REASONING_EFFORT_MODELS = {"gpt-5.4-nano", "gpt-5.4-mini"}

    # gpt-5.6* rejects function tools + any non-none reasoning on /v1/chat/completions
    # (including the server default). Force reasoning_effort="none" when tools are present.
    _FORCE_NONE_REASONING_WITH_TOOLS_PREFIX = ("gpt-5.6",)

    @property
    def _rejects_temperature(self) -> bool:
        """Whether this model rejects temperature (only accepts default=1).

        gpt-5.x models (and o-series) only support temperature=1 (the default),
        so we omit the parameter entirely.
        """
        return (
            self.actual_model.startswith(self._NO_TEMPERATURE_MODELS_PREFIX)
            or self.actual_model in self._NO_TEMPERATURE_MODELS_EXACT
        )

    @property
    def _supports_reasoning_effort(self) -> bool:
        """Whether this model accepts the reasoning_effort parameter.

        gpt-5.4-nano/mini do not support reasoning_effort with function tools
        in /v1/chat/completions, so we omit it for those models.
        """
        return (
            self._rejects_temperature and self.actual_model not in self._NO_REASONING_EFFORT_MODELS
        )

    @property
    def _requires_none_reasoning_with_tools(self) -> bool:
        """Whether tools on chat/completions require reasoning_effort='none'."""
        return self.actual_model.startswith(self._FORCE_NONE_REASONING_WITH_TOOLS_PREFIX)

    def _build_request_params(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        params = super()._build_request_params(messages, tools, stream=stream, **kwargs)

        # Newer OpenAI models take max_completion_tokens, and either reject
        # temperature (reasoning models) or reject reasoning_effort.
        params["max_completion_tokens"] = params.pop("max_tokens")
        del params["temperature"]
        if self._rejects_temperature:
            effort = kwargs.get("reasoning_effort", self.reasoning_effort)
            # gpt-5.6 + function tools on chat/completions only allows effort "none".
            if tools and self._requires_none_reasoning_with_tools:
                params["reasoning_effort"] = "none"
            elif self._supports_reasoning_effort and effort and effort != "none":
                params["reasoning_effort"] = effort
        else:
            params["temperature"] = kwargs.get("temperature", self.temperature)

        return params

    def _handle_api_error(self, exc: Exception, *, streaming: bool) -> None:
        if isinstance(exc, openai.APIError):
            self._check_chat_model_compat(exc)
        # Fall through: the original SDK exception is re-raised unwrapped.

    def count_tokens(self, text: str) -> int:
        """Count tokens using tiktoken."""
        try:
            return len(self.tokenizer.encode(text))
        except Exception:
            from coderAI.llm.base import estimate_tokens_by_chars

            return estimate_tokens_by_chars(text)
