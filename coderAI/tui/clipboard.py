"""Clipboard utilities for the TUI."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional


def copy_to_clipboard_osc52(text: str, notify_fn: Optional[Callable[[str], None]] = None) -> None:
    """Copy text to clipboard using OSC-52 escape sequences."""
    import base64
    import sys

    original_len = len(text)
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    limit = 102400
    if len(encoded) > limit:
        encoded = encoded[:limit]
        reported_len = min(original_len, (limit // 4) * 3)
    else:
        reported_len = original_len

    sys.stdout.write(f"\033]52;c;{encoded}\007")
    sys.stdout.flush()

    if notify_fn:
        if reported_len < original_len:
            notify_fn(f"Copied {reported_len:,} chars (truncated from {original_len:,}) via OSC-52")
        else:
            notify_fn(f"Copied {original_len:,} chars via OSC-52")


def copy_fallback_file(text: str, notify_fn: Optional[Callable[[str, str], None]] = None) -> None:
    """Save text to a fallback file if OSC-52 fails."""
    import tempfile

    path = Path(tempfile.gettempdir()) / "coderAI-copy.txt"
    try:
        path.write_text(text, encoding="utf-8")
        if notify_fn:
            notify_fn("info", f"Fallback: saved to {path}")
    except OSError:
        pass
