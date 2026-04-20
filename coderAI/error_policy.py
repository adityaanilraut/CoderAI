"""Error policy and retry semantics for transient LLM errors."""

import re

# Retry configuration for transient errors
MAX_RETRIES_PER_ITERATION = 3
RETRY_BASE_DELAY = 1  # seconds
MAX_CONSECUTIVE_ERRORS = 5

_TRANSIENT_PATTERNS = (
    "timeout",
    "timed out",
    "rate limit",
    "rate_limit",
    "too many requests",
    "server error",
    "internal server error",
    "connection reset",
    "connection error",
    "connect timeout",
    "overloaded",
    "capacity",
    "temporarily unavailable",
)

_TRANSIENT_HTTP_RE = re.compile(r"\b(429|500|502|503|504)\b")


def is_transient_error(exc: Exception) -> bool:
    """Determine if an exception is transient and worth retrying."""
    msg = str(exc).lower()
    if any(pattern in msg for pattern in _TRANSIENT_PATTERNS):
        return True
    return _TRANSIENT_HTTP_RE.search(msg) is not None
