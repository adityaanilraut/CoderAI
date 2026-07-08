"""DeepSeek LLM provider implementation."""

from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from coderAI.llm.base import HTTP_TOTAL_TIMEOUT, LLMProvider, REASONING_BUDGET_MAP
from coderAI.llm.base import _retry_async as _retry
from coderAI.system.safeguards import sanitize_for_log


class DeepSeekProvider(LLMProvider):
    """DeepSeek LLM provider."""

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
        """Initialize DeepSeek provider.

        Args:
            model: Model name
            api_key: DeepSeek API key
            **kwargs: Additional options (temperature, max_tokens, etc.)
        """
        super().__init__(model, api_key, **kwargs)

        if not api_key:
            raise ValueError("DeepSeek API key is required")

        self.actual_model = self.SUPPORTED_MODELS.get(model.lower(), model.lower())

        # DeepSeek uses OpenAI-compatible API
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            timeout=HTTP_TOTAL_TIMEOUT,
        )

        # DeepSeek reasoning is disabled by default
        if "reasoning_effort" not in kwargs:
            self.reasoning_effort = "none"

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
        params: Dict[str, Any] = {
            "model": self.actual_model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }

        if stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}

        if tools and self.actual_model != "deepseek-reasoner":
            params["tools"] = tools
            params["tool_choice"] = kwargs.get("tool_choice", "auto")

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

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a chat completion request to DeepSeek.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Returns:
            Response dictionary
        """
        params = self._build_request_params(messages, tools, **kwargs)

        try:

            async def _call() -> Any:
                return await self.client.chat.completions.create(**params)

            response = await _retry(_call, description="DeepSeek chat", max_retries=3)
        except Exception as e:
            raise RuntimeError(f"DeepSeek API error: {sanitize_for_log(str(e))}") from e
        result = response.model_dump()
        assert isinstance(result, dict)

        usage = result.get("usage", {})
        self.total_input_tokens += usage.get("prompt_tokens", 0)
        self.total_output_tokens += usage.get("completion_tokens", 0)

        return result

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a streaming chat completion request to DeepSeek.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Yields:
            Response chunks
        """
        params = self._build_request_params(messages, tools, stream=True, **kwargs)

        try:

            async def _create_stream() -> Any:
                return await self.client.chat.completions.create(**params)

            stream = await _retry(_create_stream, description="DeepSeek stream", max_retries=3)
        except Exception as e:
            raise RuntimeError(f"DeepSeek API streaming error: {sanitize_for_log(str(e))}") from e
        async for chunk in stream:
            chunk_data = chunk.model_dump()

            # OpenAI clients return usage in chunks when stream_options={"include_usage": True} is passed
            usage = chunk_data.get("usage")
            if usage:
                self.total_input_tokens += usage.get("prompt_tokens", 0)
                # Ensure we account for completion / reasoning tokens appropriately if provided
                completion_tokens = usage.get("completion_tokens", 0)
                self.total_output_tokens += completion_tokens

            yield chunk_data

    def supports_tools(self) -> bool:
        """DeepSeek Reasoner model does not support tool use."""
        return self.actual_model != "deepseek-reasoner"

    def clean_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """DeepSeek round-trips reasoning_content but has no vision support."""
        return self._strip_tool_images(messages)
