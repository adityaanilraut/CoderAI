"""Unified diff renderer with syntax highlighting and line indicators."""

from __future__ import annotations

from typing import Any

try:
    from rich.panel import Panel
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Panel = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


def format_diff_text(diff_text: str) -> Text | str:
    """Format a unified diff string into a syntax-highlighted Rich Text object."""
    if not _RICH or Text is None:
        return diff_text

    formatted = Text()
    lines = diff_text.splitlines()

    for line in lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            formatted.append(f"{line}\n", style="bold cyan")
        elif line.startswith("@@"):
            formatted.append(f"{line}\n", style="bold magenta")
        elif line.startswith("+"):
            formatted.append(f"{line}\n", style="green")
        elif line.startswith("-"):
            formatted.append(f"{line}\n", style="red")
        elif line.startswith("\\"):
            formatted.append(f"{line}\n", style="dim italic")
        else:
            formatted.append(f"{line}\n", style="dim white")

    return formatted


def render_diff_preview(console: Any | None, diff_text: str, title: str = "Diff Preview") -> None:
    """Render a formatted diff block inside a Rich panel or fallback to standard output."""
    if not diff_text.strip():
        return

    if console is not None and _RICH and Panel is not None:
        formatted = format_diff_text(diff_text)
        panel = Panel(
            formatted,
            title=f"[bold cyan]{title}[/]",
            border_style="cyan",
            padding=(0, 1),
        )
        console.print(panel)
    else:
        print(f"\n--- {title} ---")
        for line in diff_text.splitlines():
            print(f"  {line}")
