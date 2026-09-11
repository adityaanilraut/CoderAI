# Ported from coderai/core/common/env.py - kimi structure (kimi_cli/utils/proxy.py).
"""Proxy environment helpers.

Proxy-scheme normalization so ``socks://`` values set by tools like
V2RayN/Clash work with httpx/aiohttp, which only recognise ``socks5://``.
"""

from __future__ import annotations

import os

_PROXY_ENV_VARS = (
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
)

_SOCKS_PREFIX = "socks://"
_SOCKS5_PREFIX = "socks5://"


def normalize_proxy_env() -> None:
    """Rewrite ``socks://`` to ``socks5://`` in proxy env vars, in place."""
    for var in _PROXY_ENV_VARS:
        value = os.environ.get(var)
        if value is not None and value.lower().startswith(_SOCKS_PREFIX):
            os.environ[var] = _SOCKS5_PREFIX + value[len(_SOCKS_PREFIX) :]
