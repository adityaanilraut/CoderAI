"""Secret redaction helpers shared by error reporting and logging.

Centralizes the sensitive-key list and token-shaped-value regex so every
layer (LLM error policy, log handlers, diagnostics) redacts consistently.
"""

import logging
import re
from typing import Any, Dict

SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api-key",
        "x-api-key",
        "apiKey",
        "apikey",
        "key",
        "token",
        "secret",
        "password",
        "passwd",
        "authorization",
        "auth",
        "bearer",
        "access_token",
        "access-token",
        "accessToken",
    }
)

SENSITIVE_VALUE_RE = re.compile(
    r"(sk-(?:ant-)?[a-zA-Z0-9_-]{10,})"
    r"|(Bearer\s+[a-zA-Z0-9._\-+/=]{10,})"
    r"|([a-zA-Z0-9_-]{20,})",
    re.IGNORECASE,
)

# Narrower pattern for free-form text (log messages): only redact strings that
# look like credentials, not every long token (paths, hashes, ids would make
# logs useless).
_TEXT_SECRET_RE = re.compile(
    r"(sk-(?:ant-)?[a-zA-Z0-9_-]{10,})|(Bearer\s+[a-zA-Z0-9._\-+/=]{10,})",
    re.IGNORECASE,
)


def sanitize_dict(d: Any) -> Any:
    """Deep-sanitize a dict/list, redacting sensitive key/header values."""
    if isinstance(d, dict):
        sanitized: Dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(k, str) and k.lower() in SENSITIVE_KEYS:
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = sanitize_dict(v)
        return sanitized
    if isinstance(d, list):
        return [sanitize_dict(item) for item in d]
    if isinstance(d, str):
        if SENSITIVE_VALUE_RE.search(d):
            return "[REDACTED]"
    return d


def redact_text(text: str) -> str:
    """Redact credential-shaped substrings in free-form text, preserving the rest."""
    return _TEXT_SECRET_RE.sub("[REDACTED]", text)


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs credential-shaped tokens from records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = redact_text(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:
            # Never let redaction break logging itself.
            pass
        return True
