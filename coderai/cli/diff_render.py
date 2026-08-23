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


def parse_diff_stats(diff_text: str) -> tuple[int, int]:
    """Parse count of added and removed lines from unified diff."""
    added = 0
    removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


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
    """Render a clean, compact diff preview with syntax highlighting and line stats."""
    if not diff_text.strip():
        return

    added, removed = parse_diff_stats(diff_text)

    if console is not None and _RICH and Text is not None:
        header = Text()
        header.append("    ↳ ", style="dim cyan")
        header.append(title, style="bold cyan")
        if added > 0 or removed > 0:
            header.append(f" +{added}", style="bold green")
            header.append(f" -{removed}", style="bold red")
        console.print(header)

        diff_body = Text()
        for line in diff_text.splitlines():
            if line.startswith("--- ") or line.startswith("+++ "):
                diff_body.append(f"      {line}\n", style="bold cyan")
            elif line.startswith("@@"):
                diff_body.append(f"      {line}\n", style="bold magenta")
            elif line.startswith("+"):
                diff_body.append(f"      {line}\n", style="green")
            elif line.startswith("-"):
                diff_body.append(f"      {line}\n", style="red")
            elif line.startswith("\\"):
                diff_body.append(f"      {line}\n", style="dim italic")
            else:
                diff_body.append(f"      {line}\n", style="dim white")
        console.print(diff_body)
    else:
        stats_plain = f" (+{added}, -{removed})" if (added > 0 or removed > 0) else ""
        print(f"    ↳ {title}{stats_plain}")
        for line in diff_text.splitlines():
            print(f"      {line}")

