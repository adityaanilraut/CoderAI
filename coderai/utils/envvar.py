"""Integer environment parsing with safe defaults."""

from __future__ import annotations

import os


def get_env_int(name: str, default: int) -> int:
    """Return env var as int; ``default`` when unset or unparsable."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def get_env_float(name: str, default: float) -> float:
    """Return env var as float; ``default`` when unset or unparsable."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default
