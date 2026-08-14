"""Base LLM provider interface."""

import math
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional
from collections.abc import AsyncIterator

# Shared reasoning-effort → budget-tokens mapping used by Anthropic and DeepSeek.
REASONING_BUDGET_MAP = {"high": 16384, "medium": 8192, "low": 2048}

# Default HTTP request timeouts for aiohttp-based providers.
# - connect: fail fast on connection errors (dead server, DNS, TLS handshake)
# - sock_read: generous read window for long-running streaming LLM responses
# - total: hard ceiling to prevent unbounded hangs
HTTP_CONNECT_TIMEOUT = 10
HTTP_SOCK_READ_TIMEOUT = 120
HTTP_TOTAL_TIMEOUT = 180
DEFAULT_CONTEXT_WINDOW = 128000


class SupportedModelsView:
    """Descriptor deriving the legacy class attribute from ``ALL_SPECS``."""

    def __get__(self, instance: object, owner: type["LLMProvider"]) -> dict[str, str]:
        from coderAI.llm.registry import ALL_SPECS

        return {
            spec.id: spec.id
            for spec in ALL_SPECS
            if spec.provider_cls is owner
        }


class ModelContextWindowsView:
    """Descriptor deriving provider context limits from ``ALL_SPECS``."""

    def __get__(self, instance: object, owner: type["LLMProvider"]) -> dict[str, int]:
        from coderAI.llm.registry import ALL_SPECS

        return {
            spec.id: spec.context_window
            for spec in ALL_SPECS
            if spec.provider_cls is owner
        }

# ── Token estimation helpers ───────────────────────────────────────────────


def estimate_tokens_by_chars(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


# Canonical per-call usage schema. Every provider result surfaces usage in
# these keys so the execution loop can attribute tokens/cost without diffing
# cumulative counters or knowing which provider produced the response.
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)


def empty_usage() -> dict[str, int]:
    """Return a zeroed per-call usage dict in the canonical schema."""
    return {k: 0 for k in USAGE_KEYS}


def normalize_usage(raw: Optional[dict[str, Any]]) -> dict[str, int]:
    """Map a provider usage dict to the canonical per-call schema.

    Accepts both the OpenAI shape (``prompt_tokens`` / ``completion_tokens``)
    and the Anthropic shape (``input_tokens`` / ``output_tokens`` /
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens``). Missing
    fields default to ``0``.
    """
    raw = raw or {}
    return {
        "input_tokens": int(raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0),
        "output_tokens": int(raw.get("output_tokens", raw.get("completion_tokens", 0)) or 0),
        "cache_creation_tokens": int(
            raw.get("cache_creation_tokens", raw.get("cache_creation_input_tokens", 0)) or 0
        ),
        "cache_read_tokens": int(
            raw.get("cache_read_tokens", raw.get("cache_read_input_tokens", 0)) or 0
        ),
    }


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    # Capability flags — declared here so core never has to sniff a provider's
    # module path or class name. Providers override as needed.
    #
    # ``preserves_tool_calls_on_pause``: whether the provider round-trips the
    # assistant tool_calls of a ``pause_turn`` response on the resumed request.
    # Anthropic does (it expects the paused tool_use blocks replayed); OpenAI-
    # compatible providers do not, so the loop strips them before resuming.
    preserves_tool_calls_on_pause: bool = False
    PROVIDER_ID: ClassVar[str] = ""
    CONFIG_API_KEY: ClassVar[str | None] = None
    MODEL_PREFIX: ClassVar[str | None] = None
    CONFIG_MODEL: ClassVar[str | None] = None
    CONFIG_ENDPOINT: ClassVar[str | None] = None
    USES_REASONING_CONFIG: ClassVar[bool] = False

    SUPPORTED_MODELS = SupportedModelsView()
    MODEL_CONTEXT_WINDOWS = ModelContextWindowsView()

    def __init__(self, model: str, api_key: Optional[str] = None, **kwargs: Any):
        """Initialize the LLM provider.

        Args:
            model: Model name to use
            api_key: API key for authentication
            **kwargs: Additional provider-specific options including
                temperature, max_tokens, reasoning_effort.
        """
        self.model = model
        self.api_key = api_key
        self.options = kwargs
        self.temperature = kwargs.get("temperature", 0.7)
        self.max_tokens = kwargs.get("max_tokens", 8192)
        self.reasoning_effort = kwargs.get("reasoning_effort", "medium")
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._stream_enabled = kwargs.get("stream", True)

    @classmethod
    def from_config(cls, model: str, config: Any) -> "LLMProvider":
        """Construct this provider from the shared Config compatibility surface."""
        normalized = model.strip()
        prefix = cls.MODEL_PREFIX
        if prefix and normalized.lower().startswith(prefix + "/"):
            normalized = normalized.split("/", 1)[1]
        elif cls.CONFIG_MODEL is not None:
            normalized = str(getattr(config, cls.CONFIG_MODEL))

        kwargs: dict[str, Any] = {
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        if cls.CONFIG_API_KEY is not None:
            kwargs["api_key"] = getattr(config, cls.CONFIG_API_KEY)
        if cls.CONFIG_ENDPOINT is not None:
            kwargs["endpoint"] = getattr(config, cls.CONFIG_ENDPOINT)
        if cls.USES_REASONING_CONFIG:
            kwargs["reasoning_effort"] = config.reasoning_effort
        return cls(model=normalized, **kwargs)

    @property
    def actual_model(self) -> str:
        """The concrete API model ID this provider talks to.

        Declared on the base so core can read it without ``getattr`` guards.
        Providers that alias friendly names to canonical IDs assign
        ``self.actual_model = ...`` in their constructor (stored on
        ``_actual_model``); providers that don't fall back to ``self.model``.
        """
        return getattr(self, "_actual_model", None) or self.model

    @actual_model.setter
    def actual_model(self, value: str) -> None:
        self._actual_model = value

    @property
    def model_context_window(self) -> Optional[int]:
        """Return the provider's known model limit, or ``None`` when unknown."""
        return self.MODEL_CONTEXT_WINDOWS.get(self.actual_model)

    def get_effective_context_window(self, fallback: int = DEFAULT_CONTEXT_WINDOW) -> int:
        """Return a known model limit, safely falling back for unknown models."""
        return self.model_context_window or (fallback if fallback > 0 else DEFAULT_CONTEXT_WINDOW)

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Returns:
            Response dictionary with 'choices' containing the completion
        """
        pass

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a streaming chat completion request.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool definitions
            **kwargs: Additional request parameters

        Yields:
            Response chunks as they arrive
        """
        pass

    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in text.

        Default implementation uses a character-count heuristic (~4 chars/token).
        Providers with actual tokenizers override this.
        """
        return estimate_tokens_by_chars(text)

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the request payload for the provider's API.

        Providers override this to handle provider-specific fields
        (thinking budget, caching, etc.) while the base handles the
        common fields.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        return payload

    def supports_tools(self) -> bool:
        """Check if the provider supports tool calling.

        Returns:
            True if tools are supported
        """
        return True

    def get_model_info(self) -> dict[str, Any]:
        """Get information about the current model."""
        info: dict[str, Any] = {
            "provider": self.__class__.__name__,
            "model": self.model,
            "temperature": self.temperature if hasattr(self, "temperature") else 1.0,
        }
        if hasattr(self, "total_input_tokens"):
            info["total_input_tokens"] = self.total_input_tokens
        if hasattr(self, "total_output_tokens"):
            info["total_output_tokens"] = self.total_output_tokens
        if hasattr(self, "total_input_tokens") and hasattr(self, "total_output_tokens"):
            info["total_tokens"] = self.total_input_tokens + self.total_output_tokens
        info["cost"] = self.get_cost()
        return info

    async def close(self) -> None:
        """Clean up resources (sessions, connections, etc.).

        Override in subclasses that manage resources differently (e.g. local
        providers using aiohttp sessions).
        """
        if hasattr(self, "client"):
            await self.client.close()

    def get_cost(self) -> dict[str, Any]:
        """Get current session cost estimate.

        Returns:
            Dictionary with cost breakdown
        """
        from coderAI.system.cost import CostTracker

        actual_model: str = self.actual_model
        input_tokens: int = getattr(self, "total_input_tokens", 0)
        output_tokens: int = getattr(self, "total_output_tokens", 0)
        pricing = CostTracker.get_model_pricing(actual_model)
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_cost": round(input_cost, 6),
            "output_cost": round(output_cost, 6),
            "total_cost": round(input_cost + output_cost, 6),
            "currency": "USD",
            "model": actual_model,
        }

    def reset_usage(self) -> None:
        """Zero the provider's cumulative usage counters at a session boundary.

        The provider outlives individual sessions; the ``Agent`` is the source
        of truth for session token totals (accumulated per-call from each
        response's ``usage``). These provider counters are an additive-only
        convenience used for provider-local cost estimates and before/after
        deltas around one-off calls (summarization, sub-agent retries), so they
        are reset — never synced from the agent — when a new session starts.
        """
        if hasattr(self, "total_input_tokens"):
            self.total_input_tokens = 0
        if hasattr(self, "total_output_tokens"):
            self.total_output_tokens = 0
        if hasattr(self, "total_cache_creation_tokens"):
            self.total_cache_creation_tokens = 0
        if hasattr(self, "total_cache_read_tokens"):
            self.total_cache_read_tokens = 0

    @staticmethod
    def _strip_tool_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop internal image state for an explicitly text-only adapter.

        OpenAI-compatible adapters should normally use
        :meth:`_render_tool_images_openai` instead so image bytes are not lost.
        """
        cleaned = []
        for m in messages:
            if "tool_images" in m:
                m = {k: v for k, v in m.items() if k != "tool_images"}
            if m.get("tool_calls") and any("provider_state" in tc for tc in m["tool_calls"]):
                m = dict(m)
                m["tool_calls"] = [
                    {k: v for k, v in tc.items() if k != "provider_state"} for tc in m["tool_calls"]
                ]
            cleaned.append(m)
        return cleaned

    @staticmethod
    def _render_tool_images_openai(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Render internal tool images as OpenAI-compatible user image blocks.

        Chat-completions APIs do not permit image blocks directly on a ``tool``
        message.  Keep each consecutive tool-result group intact, then append a
        user message containing data URLs for the images returned by that group.
        This preserves tool-call ordering while delivering the actual pixels.
        """
        cleaned: list[dict[str, Any]] = []
        pending_images: list[dict[str, str]] = []

        def flush_images() -> None:
            if not pending_images:
                return
            content: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": "Images returned by the preceding tool result(s).",
                }
            ]
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image['mime_type']};base64,{image['data']}"},
                }
                for image in pending_images
            )
            cleaned.append({"role": "user", "content": content})
            pending_images.clear()

        for original in messages:
            if pending_images and original.get("role") != "tool":
                flush_images()

            message = {k: v for k, v in original.items() if k != "tool_images"}
            if message.get("tool_calls") and any(
                isinstance(call, dict) and "provider_state" in call
                for call in message["tool_calls"]
            ):
                message["tool_calls"] = [
                    {k: v for k, v in call.items() if k != "provider_state"}
                    if isinstance(call, dict)
                    else call
                    for call in message["tool_calls"]
                ]
            cleaned.append(message)

            images = original.get("tool_images")
            if isinstance(images, list):
                for image in images:
                    if not isinstance(image, dict):
                        continue
                    mime_type = image.get("mime_type")
                    data = image.get("data")
                    if (
                        isinstance(mime_type, str)
                        and mime_type.startswith("image/")
                        and isinstance(data, str)
                        and data
                    ):
                        pending_images.append({"mime_type": mime_type, "data": data})

        flush_images()
        return cleaned

    def clean_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Clean messages before sending to the API.

        By default, strips reasoning_content from assistant messages for compatibility
        with providers that reject this field. Providers that support round-tripping
        reasoning_content (DeepSeek, Gemini) MUST override this method. Anthropic
        replays signed thinking blocks through opaque provider state instead.
        The internal ``tool_images`` carrier is converted into multimodal
        OpenAI-compatible content blocks. Anthropic overrides this method and
        performs its native content-block conversion separately.
        """
        cleaned = []
        for m in messages:
            if m.get("role") == "assistant" and "reasoning_content" in m:
                m = {k: v for k, v in m.items() if k != "reasoning_content"}
            cleaned.append(m)
        return self._render_tool_images_openai(cleaned)
