# Ported from coderai/core/common/env.py - kimi structure (kimi_cli/utils/proxy.py).
"""Environment helpers (Kimi ``utils/envvar.py`` + ``utils/proxy.py`` parity).

Pure-stdlib: boolean/int env parsing with safe defaults, and proxy-scheme
normalization so ``socks://`` values set by tools like V2RayN/Clash work with
httpx/aiohttp, which only recognise ``socks5://``.
"""

from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y"})

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


def get_env_bool(name: str, default: bool = False) -> bool:
    """Return env var as bool; ``default`` when unset or unparsable."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUE_VALUES


def get_env_int(name: str, default: int) -> int:
    """Return env var as int; ``default`` when unset or unparsable."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def normalize_proxy_env() -> None:
    """Rewrite ``socks://`` to ``socks5://`` in proxy env vars, in place."""
    for var in _PROXY_ENV_VARS:
        value = os.environ.get(var)
        if value is not None and value.lower().startswith(_SOCKS_PREFIX):
            os.environ[var] = _SOCKS5_PREFIX + value[len(_SOCKS_PREFIX) :]
