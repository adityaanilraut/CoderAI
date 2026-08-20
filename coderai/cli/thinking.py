"""Thinking mode visual hierarchy and reasoning container styling."""

from __future__ import annotations

import re
from typing import Any

try:
    from rich.panel import Panel
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Panel = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


def summarize_thinking(thinking_text: str, max_chars: int = 140) -> str:
    """Extract a concise one-line summary from a reasoning block."""
    cleaned = re.sub(r"\s+", " ", thinking_text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3] + "..."


def render_thinking_block(
    console: Any | None,
    thinking_text: str,
    elapsed_seconds: float | None = None,
    expanded: bool = False,
) -> None:
    """Render a thinking mode container."""
    if not thinking_text.strip():
        return

    duration_str = f" [dim cyan]({elapsed_seconds:.1f}s)[/]" if elapsed_seconds is not None else ""

    if console is not None and _RICH:
        if expanded and Panel is not None:
            panel = Panel(
                Text(thinking_text.strip(), style="dim italic"),
                title=f"[bold magenta]Reasoning Trace[/]{duration_str}",
                border_style="magenta",
                padding=(0, 1),
            )
            console.print(panel)
        else:
            summary = summarize_thinking(thinking_text)
            console.print(
                f"  [bold magenta]Reasoning[/]{duration_str} [dim]•[/] [dim italic]{summary}[/]"
            )
    else:
        summary = summarize_thinking(thinking_text)
        duration_plain = f" ({elapsed_seconds:.1f}s)" if elapsed_seconds is not None else ""
        print(f"  Reasoning: {summary}{duration_plain}")
