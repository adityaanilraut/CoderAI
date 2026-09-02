"""Elapsed time formatting and bullet animation — ported from Kimi CLI utils/datetime.py + visualize/_blocks.py."""

from __future__ import annotations


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds for spinner/status display."""
    if seconds < 1:
        return "<1s"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# Animated bullet frames cycled every 0.13s during thinking/streaming
_BULLET_FRAMES = (".  ", ".. ", "...", " ..", "  .", "   ")
_BULLET_FRAME_INTERVAL = 0.13


def bullet_frame_for(elapsed: float) -> str:
    """Return the animated bullet frame for the given elapsed seconds."""
    idx = int(elapsed / _BULLET_FRAME_INTERVAL) % len(_BULLET_FRAMES)
    return _BULLET_FRAMES[idx]


def _estimate_tokens_float(text: str) -> float:
    """Precise float token estimate — Kimi _blocks.py:_estimate_tokens parity.

    Returns float so callers can accumulate across small chunks without floor
    truncation (e.g. 3-char ASCII chunk -> 0.75 not 0). Ranges match Kimi:
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


def estimate_tokens_float(text: str) -> float:
    """Float estimator for incremental accumulation (Kimi parity)."""
    return _estimate_tokens_float(text)


def format_token_count(n: int) -> str:
    """Format token count with commas (legacy CoderAI)."""
    return f"{n:,}"


def format_token_count_compact(n: int) -> str:
    """Compact token count — Kimi soul.format_token_count parity (1.5k, 1.2m)."""
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
    """Format context status — Kimi soul.format_context_status parity."""
    bounded = max(0.0, min(context_usage, 1.0))
    if max_context_tokens > 0:
        used = format_token_count_compact(context_tokens)
        total = format_token_count_compact(max_context_tokens)
        return f"context: {bounded:.1%} ({used}/{total})"
    return f"context: {bounded:.1%}"


# ---------------------------------------------------------------------------
# Progress bar helpers (multi-step)
# ---------------------------------------------------------------------------

PROGRESS_BAR_WIDTH = 20


def format_progress_bar(completed: int, total: int, width: int = PROGRESS_BAR_WIDTH) -> str:
    """Render a unicode progress bar like '████░░░░ 40% (2/5)'."""
    if total <= 0:
        return "░" * width + " 0%"
    pct = max(0.0, min(1.0, completed / total))
    filled = int(round(pct * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {pct * 100:.0f}% ({completed}/{total})"


# ---------------------------------------------------------------------------
# Status badges
# ---------------------------------------------------------------------------

STATUS_BADGES: dict[str, tuple[str, str]] = {
    "thinking": ("Thinking", "magenta"),
    "searching": ("Searching", "cyan"),
    "executing": ("Executing", "yellow"),
    "reading": ("Reading", "blue"),
    "writing": ("Writing", "green"),
    "compacting": ("Compacting", "yellow"),
    "queued": ("Queued", "dim"),
}

NOTIFICATION_SEVERITY_STYLE = {
    "info": "cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red",
}


def status_badge(status: str, elapsed: float | None = None) -> str:
    """Return a styled badge string like '[magenta]Thinking...[/] (3s)'."""
    label, color = STATUS_BADGES.get(status.lower(), (status, "white"))
    bullet = bullet_frame_for(elapsed) if elapsed is not None else "..."
    elapsed_str = f" ({format_elapsed(elapsed)})" if elapsed is not None and elapsed >= 1 else ""
    return f"[bold {color}]{label}{bullet}[/]{elapsed_str}"
