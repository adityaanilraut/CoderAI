"""Provider-scoped LLM retry policy for empty, rate-limit, 5xx, timeout, and transport failures.

Each retry is a fresh request over the same durable history. Failed chunks are
never persisted. Default policy: 5 retries with 500ms–10s exponential backoff
and 10% jitter.
"""

from __future__ import annotations

import random
from typing import Any

from coderai.core.common.llm_error import get_llm_error_details

RETRYABLE_CODES = ("EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT")
DEFAULT_MAX_RETRIES = 5
DEFAULT_INITIAL_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 10_000
DEFAULT_JITTER_RATIO = 0.1


def classify_llm_failure(error: Any) -> str | None:
    """Return a retryable failure code, or None if the error must not be retried."""
    details = get_llm_error_details(error)
    status = details.get("status")
    message = (details.get("message") or "").lower()
    name = (details.get("name") or "").lower()
    code = (details.get("code") or "").lower()
    combined = f"{name} {message} {code}"

    if (
        status == 429
        or "rate_limit" in code
        or "rate limit" in combined
        or "too many requests" in combined
    ):
        return "RATE_LIMIT"
    if isinstance(status, int) and 500 <= status <= 599:
        return "SERVER"
    if "timeout" in combined or "timed out" in combined:
        return "TIMEOUT"
    if any(
        token in combined
        for token in ("connection", "transport", "fetch failed", "connecterror", "apiconnection")
    ):
        return "TRANSPORT"
    return None


def is_empty_llm_response(response: dict[str, Any] | None) -> bool:
    """True when the model returned no text, tool calls, refusal, or thinking."""
    if not isinstance(response, dict):
        return True
    choice = (response.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content = msg.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    has_tools = bool(msg.get("tool_calls"))
    has_refusal = bool(msg.get("refusal"))
    thinking = msg.get("reasoning_content") or msg.get("thinking")
    has_thinking = isinstance(thinking, str) and bool(thinking.strip())
    return not (has_content or has_tools or has_refusal or has_thinking)


def retry_delay_ms(
    retry: int,
    *,
    initial_delay_ms: int = DEFAULT_INITIAL_DELAY_MS,
    max_delay_ms: int = DEFAULT_MAX_DELAY_MS,
    jitter_ratio: float = DEFAULT_JITTER_RATIO,
    random_fn: Any = random.random,
) -> float:
    """Backoff for 1-based retry number. Jitter is ±jitter_ratio around the exponential delay."""
    retry = max(1, retry)
    exponent = min(retry - 1, 10)
    exponential = min(initial_delay_ms * (2**exponent), max_delay_ms)
    jitter = 1 - jitter_ratio + 2 * jitter_ratio * float(random_fn())
    return min(exponential * jitter, max_delay_ms)
