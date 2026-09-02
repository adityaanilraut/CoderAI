"""Provider-scoped LLM retry policy for empty, rate-limit, 5xx, timeout, and transport failures.

Each retry is a fresh request over the same durable history. Failed chunks are
never persisted. Default policy: 5 retries with 500ms–10s exponential backoff
and 10% jitter.
"""

from __future__ import annotations

import random
from typing import Any

from coderai.core.common.llm_error import get_llm_error_details

RETRYABLE_CODES = (
    "EMPTY_RESPONSE",
    "RATE_LIMIT",
    "SERVER",
    "TIMEOUT",
    "TRANSPORT",
    "CONTEXT_OVERFLOW",
    "QUOTA_EXCEEDED",
)
FAILOVER_ELIGIBLE_CODES = frozenset(
    {"RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT", "CONTEXT_OVERFLOW", "QUOTA_EXCEEDED"}
)
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

    if any(
        tok in combined
        for tok in (
            "context_length_exceeded",
            "maximum context length",
            "string_above_max_length",
            "prompt is too long",
            "too many tokens",
            "context length",
            "max_tokens",
        )
    ):
        return "CONTEXT_OVERFLOW"
    if any(
        tok in combined
        for tok in (
            "insufficient_quota",
            "quota_exceeded",
            "credit_balance_too_low",
            "billing_not_active",
            "exceeded your current quota",
        )
    ):
        return "QUOTA_EXCEEDED"
    if (
        status == 429
        or "rate_limit" in code
        or "rate limit" in combined
        or "too many requests" in combined
    ):
        return "RATE_LIMIT"
    if (isinstance(status, int) and 500 <= status <= 599) or any(
        tok in combined
        for tok in (
            "500",
            "502",
            "503",
            "504",
            "internal server error",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "server_error",
        )
    ):
        return "SERVER"

    if "timeout" in combined or "timed out" in combined:
        return "TIMEOUT"
    if any(
        token in combined
        for token in ("connection", "transport", "fetch failed", "connecterror", "apiconnection")
    ):
        return "TRANSPORT"
    return None


def is_failover_eligible(error: Any) -> bool:
    """Check if an LLM error qualifies for multi-model failover cascade."""
    code = classify_llm_failure(error)
    return code in FAILOVER_ELIGIBLE_CODES


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


def provider_retry_after_ms(error: Any) -> float | None:
    """Parse a provider ``Retry-After`` header (seconds or HTTP-date), or None.

    Mirrors the harness ``providerRetryAfterMs``: honored when a positive
    finite value; callers treat an over-cap value as a give-up in normal mode.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    if not isinstance(headers, dict):
        try:
            headers = dict(headers)
        except Exception:
            return None
    raw = headers.get("retry-after-ms") or headers.get("Retry-After-Ms")
    if raw is None:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(str(raw))
        if not (0 < value <= 1_000_000):
            return None
        return value
    except (TypeError, ValueError):
        pass
    # HTTP-date form (RFC 7231)
    try:
        import datetime
        import email.utils

        parsed = email.utils.parsedate_to_datetime(str(raw))
        now = datetime.datetime.now(datetime.timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        delay_ms = (parsed - now).total_seconds() * 1000.0
        return delay_ms if 0 < delay_ms <= 1_000_000 else None
    except Exception:
        return None
