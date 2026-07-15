"""Base class for cloud LLM providers with OpenAI-compatible SDK clients."""

from typing import Any, AsyncIterator, Dict, List, Optional

from coderAI.llm.base import LLMProvider
from coderAI.llm.base import _retry_async as _retry
from coderAI.system.redaction import redact_text


class OpenAICompatibleCloudProvider(LLMProvider):
    """Base class for cloud providers whose SDK mirrors ``openai.AsyncOpenAI``.

    Owns ``chat()`` and ``stream()``: build request params, retry the SDK
    call, track usage, return/yield ``model_dump()`` dicts. Subclasses set
    ``self.client`` in their constructor and override the small hooks for
    provider quirks (request params, stream-usage extraction, error shaping).
    """

    # Client with an OpenAI-compatible ``chat.completions.create``
    # (``openai.AsyncOpenAI``, ``groq.AsyncGroq``, ...). Set by subclass
    # constructors after ``super().__init__`` validates the API key.
    client: Any

    SUPPORTED_MODELS: Dict[str, str] = {}

    # Human-readable name used in error messages and retry logs.
    PROVIDER_LABEL = "LLM"

    # Whether streaming requests ask the API to attach usage to chunks
    # (``stream_options={"include_usage": True}``). Off for APIs that
    # reject the parameter or report usage elsewhere.
    STREAM_INCLUDES_USAGE = False

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        super().__init__(model, api_key, **kwargs)
        if not api_key:
            raise ValueError(f"{self.PROVIDER_LABEL} API key is required")
        self.actual_model = self._resolve_model(model)

    def _resolve_model(self, model: str) -> str:
        """Map a friendly model name to the concrete API model ID."""
        return self.SUPPORTED_MODELS.get(model, model)

    def _build_request_params(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "model": self.actual_model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        if stream:
            params["stream"] = True
            if self.STREAM_INCLUDES_USAGE:
                params["stream_options"] = {"include_usage": True}

        if tools:
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

        return params

    def _handle_api_error(self, exc: Exception, *, streaming: bool) -> None:
        """Shape a failed request into the provider's public error.

        Default wraps in ``RuntimeError``. Overrides may log first, raise a
        different error, or return normally to re-raise the original.
        """
        verb = "streaming error" if streaming else "error"
        raise RuntimeError(f"{self.PROVIDER_LABEL} API {verb}: {redact_text(str(exc))}") from exc

    def _extract_stream_usage(self, chunk_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Pull the usage dict out of a stream chunk (provider-specific shape)."""
        usage = chunk_data.get("usage")
        return usage if isinstance(usage, dict) else None

    def _track_usage(self, usage: Dict[str, Any]) -> None:
        self.total_input_tokens += usage.get("prompt_tokens", 0)
        self.total_output_tokens += usage.get("completion_tokens", 0)

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        params = self._build_request_params(messages, tools, **kwargs)

        async def _call() -> Any:
            return await self.client.chat.completions.create(**params)

        try:
            response = await _retry(_call, description=f"{self.PROVIDER_LABEL} chat")
        except Exception as e:
            self._handle_api_error(e, streaming=False)
            raise
        result = response.model_dump()
        assert isinstance(result, dict)

        self._track_usage(result.get("usage") or {})
        return result

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        params = self._build_request_params(messages, tools, stream=True, **kwargs)

        async def _create_stream() -> Any:
            return await self.client.chat.completions.create(**params)

        try:
            stream = await _retry(_create_stream, description=f"{self.PROVIDER_LABEL} stream")
        except Exception as e:
            self._handle_api_error(e, streaming=True)
            raise
        async for chunk in stream:
            chunk_data = chunk.model_dump()
            usage = self._extract_stream_usage(chunk_data)
            if usage:
                self._track_usage(usage)
            yield chunk_data
