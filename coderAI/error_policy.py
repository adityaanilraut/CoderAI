"""Error policy and retry semantics for transient LLM errors."""

import json as _json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """Raised when the configured budget has been exhausted.

    Distinct from transient errors so the retry loop never swallows it:
    burning retries on a hard stop is pure waste and produces misleading
    "transient error" logs. Caught at the iteration boundary where it is
    turned into a terminal user-facing message.
    """


# Retry configuration for transient errors
MAX_RETRIES_PER_ITERATION = 3
RETRY_BASE_DELAY = 1  # seconds
RETRY_BACKOFF_FACTOR = 2
RETRY_MAX_DELAY = 60  # seconds — cap to avoid unreasonable waits
MAX_CONSECUTIVE_ERRORS = 5

# Cap on consecutive ``pause_turn`` finish_reasons. Each ``pause_turn`` decrements
# the iteration counter (the model was thinking, not progressing), so without
# a separate cap a buggy provider could pin the loop at iteration=0 forever.
MAX_CONSECUTIVE_PAUSES = 10

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
    "getaddrinfo",
    "name or service not known",
    "bad handshake",
    "overloaded",
    "capacity",
    "temporarily unavailable",
    "throttled",
    "exhausted",
    "unavailable",
)

_TRANSIENT_HTTP_RE = re.compile(r"\b(429|500|502|503|504)\b")


def is_transient_error(exc: Exception) -> bool:
    """Determine if an exception is transient and worth retrying.

    Checks string patterns in the error message and looks for HTTP
    status codes that indicate transient server-side issues.
    """
    if isinstance(exc, BudgetExceededError):
        return False
    msg = str(exc).lower()
    if any(pattern in msg for pattern in _TRANSIENT_PATTERNS):
        return True
    if _TRANSIENT_HTTP_RE.search(msg):
        return True
    # Check for structured error bodies (e.g. OpenAI-style JSON errors)
    body = _try_extract_response_body(exc)
    if body:
        body_msg = _json.dumps(body).lower() if isinstance(body, dict) else str(body).lower()
        if any(pattern in body_msg for pattern in _TRANSIENT_PATTERNS):
            return True
        if isinstance(body, dict):
            if body.get("type") == "error":
                error_code = body.get("error", {}).get("type", "")
                if "rate_limit" in error_code or "too_many_requests" in error_code:
                    return True
            code = body.get("code", "")
            if isinstance(code, str) and ("exhausted" in code or "unavailable" in code):
                return True
    # Check for `isRetryable` / `is_retryable` flag on the exception
    if getattr(exc, "is_retryable", None) is True:
        return True
    return False


def compute_retry_delay(exc: Exception, attempt: int) -> float:
    """Compute a retry delay in seconds for a given exception and attempt.

    Honors ``Retry-After`` and ``Retry-After-Ms`` HTTP headers when
    they are present on the exception. Falls back to exponential
    backoff: ``RETRY_BASE_DELAY * RETRY_BACKOFF_FACTOR^(attempt-1)``,
    capped at ``RETRY_MAX_DELAY``.

    This mirrors OpenCode's header-aware retry logic.
    """
    # Try HTTP response headers first
    headers = _try_extract_headers(exc)
    if headers:
        # Retry-After in milliseconds (non-standard but used by some APIs)
        retry_after_ms = headers.get("retry-after-ms") or headers.get("Retry-After-Ms")
        if retry_after_ms is not None:
            try:
                ms = float(retry_after_ms)
                if not (ms != ms) and ms > 0:  # NaN check
                    return min(ms / 1000.0, RETRY_MAX_DELAY)
            except (ValueError, TypeError):
                pass

        # Standard Retry-After header (seconds or HTTP-date)
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after is not None:
            try:
                seconds = float(retry_after)
                if not (seconds != seconds) and seconds > 0:  # NaN check
                    return min(seconds, RETRY_MAX_DELAY)
            except (ValueError, TypeError):
                # Try parsing as HTTP-date
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(retry_after)
                    now_dt = None
                    try:
                        from datetime import datetime, timezone
                        now_dt = datetime.now(timezone.utc)
                    except ImportError:
                        from datetime import datetime
                        now_dt = datetime.utcnow()
                    delta = (dt - now_dt).total_seconds()
                    if delta > 0:
                        return min(delta, RETRY_MAX_DELAY)
                except Exception:
                    pass

    # Fall back to exponential backoff
    delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
    return min(delay, RETRY_MAX_DELAY)


def _try_extract_headers(exc: Exception) -> Optional[Dict[str, str]]:
    """Extract HTTP response headers from an exception, if available.

    Supports aiohttp (``ClientResponseError.headers``, ``ContentTypeError.headers``),
    the openai library (``APIStatusError.response.headers``), and requests-style
    exceptions.

    Returns ``None`` when no headers could be found.
    """
    # aiohttp ClientResponseError
    if hasattr(exc, "headers"):
        h = exc.headers
        if isinstance(h, dict):
            return h

    # openai APIStatusError / httpx Response
    for attr in ("response", "resp"):
        resp = getattr(exc, attr, None)
        if resp is None:
            continue
        h = getattr(resp, "headers", None)
        if h is not None:
            if isinstance(h, dict):
                return h
            if hasattr(h, "items"):
                try:
                    return dict(h.items())
                except Exception:
                    pass
    return None


def _try_extract_response_body(exc: Exception) -> Optional[Any]:
    """Try to extract a JSON response body from an exception."""
    for attr in ("response", "resp"):
        resp = getattr(exc, attr, None)
        if resp is None:
            continue
        body = None
        if hasattr(resp, "json") and callable(resp.json):
            try:
                body = resp.json()
            except Exception:
                pass
        if body is None and hasattr(resp, "content"):
            try:
                raw = resp.content
                if isinstance(raw, (str, bytes)):
                    body = _json.loads(raw)
            except Exception:
                pass
        if body is not None:
            return body
    return None
