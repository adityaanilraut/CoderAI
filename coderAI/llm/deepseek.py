"""DeepSeek LLM provider implementation."""

from typing import Any, Optional

from openai import AsyncOpenAI

from coderAI.llm.base import HTTP_TOTAL_TIMEOUT, REASONING_BUDGET_MAP
from coderAI.llm.cloud_base import OpenAICompatibleCloudProvider


class DeepSeekProvider(OpenAICompatibleCloudProvider):
    """DeepSeek LLM provider (OpenAI-compatible API)."""

    PROVIDER_ID = "deepseek"
    CONFIG_API_KEY = "deepseek_api_key"
    MODEL_PREFIX = "deepseek"
    PROVIDER_LABEL = "DeepSeek"
    STREAM_INCLUDES_USAGE = True

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        super().__init__(model, api_key, **kwargs)

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            timeout=HTTP_TOTAL_TIMEOUT,
        )

        # DeepSeek reasoning is disabled by default
        if "reasoning_effort" not in kwargs:
            self.reasoning_effort = "none"

    def _resolve_model(self, model: str) -> str:
        from coderAI.llm.registry import resolve_alias

        return resolve_alias(model).lower()

    @property
    def _uses_v4_family(self) -> bool:
        return self.actual_model in {"deepseek-v4-flash", "deepseek-v4-pro"}

    def _build_request_params(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.actual_model == "deepseek-reasoner":
            tools = None
        params = super()._build_request_params(messages, tools, stream=stream, **kwargs)

        # DeepSeek V4 supports a thinking mode that produces reasoning_content
        # in the response. The agent loop now round-trips reasoning_content
        # across tool turns via session persistence and clean_messages,
        # so we enable thinking by default for V4-compatible models.
        if self._uses_v4_family:
            effort = kwargs.get("reasoning_effort", self.reasoning_effort)
            if effort and effort != "none":
                budget = REASONING_BUDGET_MAP.get(effort, 8192)
                params["extra_body"] = {"thinking": {"type": "enabled", "budget_tokens": budget}}
            else:
                params["extra_body"] = {"thinking": {"type": "disabled"}}

        return params

    def supports_tools(self) -> bool:
        """DeepSeek Reasoner model does not support tool use."""
        return self.actual_model != "deepseek-reasoner"

    def clean_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Round-trip reasoning and render tool images for compatible models."""
        return self._render_tool_images_openai(messages)
