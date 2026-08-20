"""Pluggable web-search providers (dsh web subsystem). Default: existing CoderAI stack."""

from __future__ import annotations

import os
from typing import Any
from collections.abc import Callable

WebSearchFn = Callable[[str, dict[str, Any]], Any]

_PROVIDERS: dict[str, WebSearchFn] = {}


def register_web_search_provider(name: str, fn: WebSearchFn) -> None:
    _PROVIDERS[name.strip().lower()] = fn


def list_web_search_providers() -> list[str]:
    return sorted(_PROVIDERS)


def resolve_web_search_provider(name: str | None) -> WebSearchFn | None:
    if not name:
        name = os.environ.get("CODERAI_WEB_SEARCH_PROVIDER") or os.environ.get(
            "CODERAI_WEB_SEARCH_TOOL"
        )
    if not name:
        return None
    return _PROVIDERS.get(str(name).strip().lower())
