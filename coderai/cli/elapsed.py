"""Elapsed time formatting, bullet animation, and token estimation utilities."""

from __future__ import annotations

# Single source of truth: `format_elapsed` lives in coderai.utils.datetime and is
# re-exported here for the UI consumers that import it from this module.
from coderai.utils.datetime import format_elapsed as format_elapsed


# Animated bullet frames cycled every 0.13s during thinking/streaming
_BULLET_FRAMES = (".  ", ".. ", "...", " ..", "  .", "   ")
_BULLET_FRAME_INTERVAL = 0.13


def bullet_frame_for(elapsed: float) -> str:
    """Return the animated bullet frame for the given elapsed seconds."""
    idx = int(elapsed / _BULLET_FRAME_INTERVAL) % len(_BULLET_FRAMES)
    return _BULLET_FRAMES[idx]


def _estimate_tokens_float(text: str) -> float:
    """Precise float token estimate.

    Returns float so callers can accumulate across small chunks without floor
    truncation (e.g. 3-char ASCII chunk -> 0.75 not 0):
    CJK Unified/ExtA/Compat/Symbols/Fullwidth -> 1.5 per char, latin -> 0.25.
    """
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0xF900 <= cp <= 0xFAFF
            or 0x3000 <= cp <= 0x303F
            or 0xFF00 <= cp <= 0xFFEF
        ):
            cjk += 1
        else:
            other += 1
    return cjk * 1.5 + other / 4


def estimate_tokens(text: str) -> int:
    """Backward-compat int wrapper around float estimator."""
    return int(_estimate_tokens_float(text))


def format_token_count_compact(n: int) -> str:
    """Compact token count (1.5k, 1.2m)."""
    if n >= 1_000_000:
        v = n / 1_000_000
        suf = "m"
    elif n >= 1_000:
        v = n / 1_000
        suf = "k"
    else:
        return str(n)
    compact = f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{compact}{suf}"


def format_context_status(
    context_usage: float, context_tokens: int = 0, max_context_tokens: int = 0
) -> str:
    """Format context status."""
    bounded = max(0.0, min(context_usage, 1.0))
    if max_context_tokens > 0:
        used = format_token_count_compact(context_tokens)
        total = format_token_count_compact(max_context_tokens)
        return f"context: {bounded:.1%} ({used}/{total})"
    return f"context: {bounded:.1%}"


NOTIFICATION_SEVERITY_STYLE = {
    "info": "cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red",
}
