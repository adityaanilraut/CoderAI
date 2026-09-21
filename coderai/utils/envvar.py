"""Environment variable helpers.

Pure-stdlib: boolean/int env parsing with safe defaults.
"""

from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y"})


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
