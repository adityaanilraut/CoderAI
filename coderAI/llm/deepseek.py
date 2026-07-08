"""DeepSeek LLM provider implementation."""

from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from coderAI.llm.base import HTTP_TOTAL_TIMEOUT, REASONING_BUDGET_MAP
from coderAI.llm.cloud_base import OpenAICompatibleCloudProvider


class DeepSeekProvider(OpenAICompatibleCloudProvider):
    """DeepSeek LLM provider (OpenAI-compatible API)."""

    PROVIDER_LABEL = "DeepSeek"
    STREAM_INCLUDES_USAGE = True

    SUPPORTED_MODELS = {
        "deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-chat": "deepseek-chat",
        "deepseek-reasoner": "deepseek-reasoner",
        "deepseek-v3": "deepseek-chat",
        "deepseek-v3.2": "deepseek-chat-v3.2",
        "deepseek-r1": "deepseek-reasoner",
    }

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
        return self.SUPPORTED_MODELS.get(model.lower(), model.lower())

    @property
    def _uses_v4_family(self) -> bool:
        return self.actual_model in {"deepseek-v4-flash", "deepseek-v4-pro"}

    def _build_request_params(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if self.actual_model == "deepseek-reasoner":
            tools = None
        params = super()._build_request_params(messages, tools, stream=stream, **kwargs)

        # DeepSeek V4 supports a thinking mode that produces reasoning_content
        # in the response. The agent loop now round-trips reasoning_content
        # across tool turns via session persistence and clean_messages,
        # so we enable thinking by default for V4-compatible models.
        if self._uses_v4_family:
            if self.reasoning_effort and self.reasoning_effort != "none":
                budget = REASONING_BUDGET_MAP.get(self.reasoning_effort, 8192)
                params["extra_body"] = {"thinking": {"type": "enabled", "budget_tokens": budget}}
            else:
                params["extra_body"] = {"thinking": {"type": "disabled"}}

        return params

    def supports_tools(self) -> bool:
        """DeepSeek Reasoner model does not support tool use."""
        return self.actual_model != "deepseek-reasoner"

    def clean_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """DeepSeek round-trips reasoning_content but has no vision support."""
        return self._strip_tool_images(messages)
