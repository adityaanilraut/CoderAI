"""Plan checklist and progress renderer for Plan Mode and UpdatePlan tool."""

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


def parse_plan_stats(plan_text: str) -> tuple[int, int]:
    """Parse total and completed task count from markdown checklist."""
    total = 0
    completed = 0
    for line in plan_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [x]", "* [x]", "- [X]", "* [X]")):
            total += 1
            completed += 1
        elif stripped.startswith(("- [ ]", "* [ ]")):
            total += 1
    return total, completed


def make_plan_progress_bar(completed: int, total: int, width: int = 10) -> str:
    """Generate a visual progress bar string for plans."""
    if total <= 0:
        return ""
    pct = max(0.0, min(1.0, completed / total))
    filled = int(round(pct * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {int(pct * 100)}%"


def format_plan_content(plan_text: str) -> Text | str:
    """Format markdown checklist into styled Rich Text."""
    if not _RICH or Text is None:
        return plan_text

    formatted = Text()
    lines = plan_text.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            formatted.append("\n")
            continue

        if stripped.startswith(("- [x]", "* [x]", "- [X]", "* [X]")):
            item_text = stripped[5:].strip()
            formatted.append("  ✓ ", style="bold green")
            formatted.append(f"{item_text}\n", style="green")
        elif stripped.startswith(("- [>]", "* [>]", "- [*]", "* [*]")):
            item_text = stripped[5:].strip()
            formatted.append("  ❯ ", style="bold cyan")
            formatted.append(f"{item_text}\n", style="bold cyan")
        elif stripped.startswith(("- [-]", "* [-]")):
            item_text = stripped[5:].strip()
            formatted.append("  - ", style="bold yellow")
            formatted.append(f"{item_text}\n", style="yellow")
        elif stripped.startswith(("- [ ]", "* [ ]")):
            item_text = stripped[5:].strip()
            formatted.append("  ○ ", style="bold cyan")
            formatted.append(f"{item_text}\n", style="white")
        elif stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            formatted.append(f"\n  {heading}\n", style="bold yellow")
        elif stripped.startswith(("-", "*", "•")):
            item_text = stripped.lstrip("-*• ").strip()
            formatted.append("    • ", style="dim cyan")
            formatted.append(f"{item_text}\n", style="dim white")
        else:
            formatted.append(f"  {stripped}\n", style="dim white")

    return formatted


def render_plan_preview(console: Any | None, plan_text: str, title: str = "Plan Progress") -> None:
    """Render the plan progress checklist as a compact sequential terminal event."""
    if not plan_text.strip():
        return

    total, completed = parse_plan_stats(plan_text)
    bar_str = make_plan_progress_bar(completed, total, width=8)
    progress_badge = f" {bar_str} ({completed}/{total} tasks)" if total > 0 else ""

    if console is not None and _RICH and Text is not None:
        header = Text()
        header.append("    ↳ ", style="dim yellow")
        header.append(title, style="bold yellow")
        if progress_badge:
            header.append(f" {progress_badge}", style="dim")
        console.print(header)

        formatted = format_plan_content(plan_text)
        console.print(formatted)
    else:
        progress_plain = f" {progress_badge}" if progress_badge else ""
        print(f"    ↳ {title}{progress_plain}")
        for line in plan_text.splitlines():
            print(f"      {line}")

