"""Base LLM provider interface."""

import asyncio
import logging
import math
import random
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

# Shared reasoning-effort → budget-tokens mapping used by Anthropic and DeepSeek.
REASONING_BUDGET_MAP = {"high": 16384, "medium": 8192, "low": 2048}

# Default HTTP request timeouts for aiohttp-based providers.
# - connect: fail fast on connection errors (dead server, DNS, TLS handshake)
# - sock_read: generous read window for long-running streaming LLM responses
# - total: hard ceiling to prevent unbounded hangs
HTTP_CONNECT_TIMEOUT = 10
HTTP_SOCK_READ_TIMEOUT = 120
HTTP_TOTAL_TIMEOUT = 180

logger = logging.getLogger(__name__)

# ── Retry helpers ──────────────────────────────────────────────────────────

_RETRYABLE_STATUSES = frozenset({429, 502, 503})
_MAX_RETRIES = 3


def _exponential_backoff_sleep(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 8.0,
    jitter: float = 0.3,
) -> float:
    delay = min(base * (2 ** (attempt - 1)), cap)
    delay += random.uniform(0, delay * jitter)
    return float(delay)


async def _retry_async(
    fn: Callable[[], Any],
    *,
    max_retries: int = _MAX_RETRIES,
    description: str = "LLM request",
    is_retryable: Optional[Callable[[Exception], bool]] = None,
) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 2):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt > max_retries:
                raise
            retryable = is_retryable(exc) if is_retryable else _default_retryable(exc)
            if not retryable:
                raise
            delay = _exponential_backoff_sleep(attempt)
            logger.warning(
                "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                description,
                attempt,
                max_retries,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _default_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, (int, float)):
        return int(status) in _RETRYABLE_STATUSES
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "429",
            "502",
            "503",
            "rate limit",
            "too many requests",
            "service unavailable",
            "bad gateway",
            "server error",
        )
    )


# ── Token estimation helpers ───────────────────────────────────────────────


def estimate_tokens_by_chars(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

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

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
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
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
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
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Build the request payload for the provider's API.

        Providers override this to handle provider-specific fields
        (thinking budget, caching, etc.) while the base handles the
        common fields.
        """
        payload: Dict[str, Any] = {
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

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        info: Dict[str, Any] = {
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

    def get_cost(self) -> Dict[str, Any]:
        """Get current session cost estimate.

        Returns:
            Dictionary with cost breakdown
        """
        from coderAI.system.cost import CostTracker

        actual_model: str = getattr(self, "actual_model", self.model)
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

    def set_cumulative_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Realign cumulative usage counters after a model switch or reset.

        Providers track different subsets of these (Anthropic exposes cache
        counters, others don't), so each attribute is updated only when the
        provider already defines it.
        """
        if hasattr(self, "total_input_tokens"):
            self.total_input_tokens = max(0, int(input_tokens or 0))
        if hasattr(self, "total_output_tokens"):
            self.total_output_tokens = max(0, int(output_tokens or 0))
        if hasattr(self, "total_cache_creation_tokens"):
            self.total_cache_creation_tokens = max(0, int(cache_creation_tokens or 0))
        if hasattr(self, "total_cache_read_tokens"):
            self.total_cache_read_tokens = max(0, int(cache_read_tokens or 0))

    @staticmethod
    def _strip_tool_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drop the ``tool_images`` vision carrier from tool messages.

        Only the Anthropic provider renders base64 images inside a tool result;
        every other provider would reject the unknown key (or has no way to use
        it), so it is stripped before the request. Providers that override
        ``clean_messages`` (DeepSeek, Gemini) must call this to stay safe.
        """
        cleaned = []
        for m in messages:
            if "tool_images" in m:
                m = {k: v for k, v in m.items() if k != "tool_images"}
            cleaned.append(m)
        return cleaned

    def clean_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Clean messages before sending to the API.

        By default, strips reasoning_content from assistant messages for compatibility
        with providers that reject this field. Providers that support round-tripping
        reasoning_content (DeepSeek, Gemini, Anthropic) MUST override this method.
        The ``tool_images`` vision carrier is always stripped here (Anthropic
        overrides to keep it).
        """
        cleaned = []
        for m in messages:
            if m.get("role") == "assistant" and "reasoning_content" in m:
                m = {k: v for k, v in m.items() if k != "reasoning_content"}
            cleaned.append(m)
        return self._strip_tool_images(cleaned)
