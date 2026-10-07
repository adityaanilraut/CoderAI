"""Presentation-only redaction. Never mutate the resolved configuration."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET = re.compile(r"key|token|secret|password|credential|auth|bearer|cookie", re.I)


def redact_settings(value: Any, key: str = "") -> Any:
    if _SECRET.search(key) and isinstance(value, (dict, list)):
        # Credential containers may have arbitrary inner names.
        return "[redacted]" if value else value
    if _SECRET.search(key) and not isinstance(value, (dict, list)):
        return "[redacted]" if value else value
    if isinstance(value, dict):
        return {str(k): redact_settings(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_settings(v, key) for v in value]
    if isinstance(value, str) and "://" in value:
        try:
            url = urlsplit(value)
            host = url.netloc.rsplit("@", 1)[-1]
            query = urlencode(
                [
                    (k, "[redacted]" if _SECRET.search(k) else v)
                    for k, v in parse_qsl(url.query, keep_blank_values=True)
                ]
            )
            fragment = url.fragment
            if "=" in fragment:
                fragment = urlencode(
                    [
                        (k, "[redacted]" if _SECRET.search(k) else v)
                        for k, v in parse_qsl(fragment, keep_blank_values=True)
                    ]
                )
            return urlunsplit((url.scheme, host, url.path, query, fragment))
        except ValueError:
            return "[invalid URL]"
    return value
