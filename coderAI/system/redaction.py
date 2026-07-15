"""Authoritative credential redaction for config, diagnostics, and logging."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Kept as a public constant for callers that imported the old exact-key set.
# ``is_sensitive_key`` is authoritative and additionally handles provider-
# prefixed fields such as ``openai_api_key`` and ``vendor_access_token``.
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api-key",
        "apikey",
        "x-api-key",
        "authorization",
        "proxy-authorization",
        "auth",
        "bearer",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "secret",
        "client_secret",
        "password",
        "passwd",
    }
)

_SENSITIVE_KEY_SUFFIX_RE = re.compile(
    r"(?:^|_)(?:api_key|token|access_token|refresh_token|id_token|auth_token|"
    r"secret|client_secret|password|passwd)$"
)

# Prefix-specific patterns avoid treating ordinary hashes, UUIDs, and model IDs
# as credentials. Key/value text patterns below cover opaque provider keys that
# do not have a stable public prefix.
SENSITIVE_VALUE_RE = re.compile(
    r"\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{10,}\b"
    r"|\bgsk_[A-Za-z0-9_-]{10,}\b"
    r"|\bAIza[A-Za-z0-9_-]{20,}\b"
    r"|\bLLM\|[A-Za-z0-9._~+\-/=]{10,}"
    r"|\btvly-(?:dev-|prod-)?[A-Za-z0-9_-]{10,}\b"
    r"|\bexa-[A-Za-z0-9_-]{10,}\b"
    r"|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+\-/=]{8,}",
    re.IGNORECASE,
)

_TEXT_KEY = (
    r"(?:[A-Za-z0-9]+[_-])*api[_-]?key|apikey|x-api-key|authorization|"
    r"proxy-authorization|auth|(?:access[_-]?|refresh[_-]?|id[_-]?|auth[_-]?)?token|"
    r"(?:client[_-]?)?secret|password|passwd"
)
_QUOTED_KEY_VALUE_RE = re.compile(
    rf"(?P<prefix>['\"]?(?:{_TEXT_KEY})['\"]?\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_UNQUOTED_KEY_VALUE_RE = re.compile(
    rf"(?P<prefix>\b(?:{_TEXT_KEY})\b\s*[:=]\s*)"
    r"(?P<value>(?:(?:Bearer|Basic)\s+)?[^\s,;}\]]+)",
    re.IGNORECASE,
)


def is_sensitive_key(key: object) -> bool:
    """Return whether a mapping/config key conventionally contains a secret."""
    if not isinstance(key, str):
        return False
    snake_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_key.lower()).strip("_")
    if key.lower() in SENSITIVE_KEYS or normalized in {
        "api_key",
        "apikey",
        "x_api_key",
        "authorization",
        "proxy_authorization",
        "auth",
        "bearer",
        "token",
    }:
        return True
    return bool(_SENSITIVE_KEY_SUFFIX_RE.search(normalized))


def redact_text(text: str) -> str:
    """Redact credentials and secret key/value pairs from free-form text."""

    def replace_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{REDACTED}{quote}"

    text = _QUOTED_KEY_VALUE_RE.sub(replace_quoted, text)
    text = _UNQUOTED_KEY_VALUE_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    return SENSITIVE_VALUE_RE.sub(REDACTED, text)


def redact_secrets(value: Any) -> Any:
    """Recursively return a copy of *value* with credentials fully redacted.

    Mapping values are redacted based on their key at every depth. Strings in
    otherwise non-sensitive fields are scanned for credential prefixes and
    key/value text, while ordinary hashes, UUIDs, and model IDs are preserved.
    """
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key) and item not in (None, ""):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, set):
        return {redact_secrets(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(redact_secrets(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def sanitize_dict(value: Any) -> Any:
    """Backward-compatible alias for :func:`redact_secrets`."""
    return redact_secrets(value)


def sanitize_for_log(text: str) -> str:
    """Backward-compatible text sanitizer routed through the central API."""
    return redact_text(text)


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs credentials from messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Render first so redacting a ``token=%s`` format string cannot
            # remove its placeholder while leaving an argument behind.
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:
            # Never let redaction break logging itself.
            pass
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that also redacts exception and traceback text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))
