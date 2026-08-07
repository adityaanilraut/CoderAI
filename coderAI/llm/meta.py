"""Meta Model API LLM provider (OpenAI-compatible Chat Completions)."""

from typing import Any, Optional

from openai import AsyncOpenAI

from coderAI.llm.base import HTTP_TOTAL_TIMEOUT
from coderAI.llm.cloud_base import OpenAICompatibleCloudProvider


class MetaProvider(OpenAICompatibleCloudProvider):
    """Meta Model API provider for Muse Spark models."""

    PROVIDER_LABEL = "Meta"
    STREAM_INCLUDES_USAGE = True

    SUPPORTED_MODELS = {
        "muse-spark-1.2": "muse-spark-1.2",
        "muse-spark-1.1": "muse-spark-1.1",
        "muse-spark-1.2-contributor": "muse-spark-1.2-contributor",
    }
    MODEL_CONTEXT_WINDOWS = {
        "muse-spark-1.2": 1_048_576,
        "muse-spark-1.1": 1_048_576,
        "muse-spark-1.2-contributor": 1_048_576,
    }

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        super().__init__(model, api_key, **kwargs)

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.meta.ai/v1",
            timeout=HTTP_TOTAL_TIMEOUT,
        )

    def _resolve_model(self, model: str) -> str:
        from coderAI.llm.registry import resolve_alias

        return resolve_alias(model).lower()

    def _build_request_params(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        params = super()._build_request_params(messages, tools, stream=stream, **kwargs)

        # Muse Spark always reasons; sending reasoning_effort="none" returns HTTP 400.
        # Omit the param when effort is none/empty so the API uses its default.
        effort = kwargs.get("reasoning_effort", self.reasoning_effort)
        if effort and effort != "none":
            params["reasoning_effort"] = effort

        return params
