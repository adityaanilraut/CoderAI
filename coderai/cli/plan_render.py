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
        elif stripped.startswith(("- [ ]", "* [ ]")):
            item_text = stripped[5:].strip()
            formatted.append("  ○ ", style="bold cyan")
            formatted.append(f"{item_text}\n", style="white")
        elif stripped.startswith("#"):
            formatted.append(f"\n{stripped}\n", style="bold yellow")
        else:
            formatted.append(f"  {stripped}\n", style="dim white")

    return formatted


def render_plan_preview(console: Any | None, plan_text: str, title: str = "Plan Progress") -> None:
    """Render the plan progress checklist inside a Rich panel or fallback to standard output."""
    if not plan_text.strip():
        return

    total, completed = parse_plan_stats(plan_text)
    pct = int((completed / total) * 100) if total > 0 else 0
    progress_badge = f" • [{completed}/{total} tasks ({pct}%)]" if total > 0 else ""

    if console is not None and _RICH and Panel is not None:
        formatted = format_plan_content(plan_text)
        panel = Panel(
            formatted,
            title=f"[bold yellow]{title}[/][dim]{progress_badge}[/]",
            border_style="yellow",
            padding=(0, 1),
        )
        console.print(panel)
    else:
        print(f"\n--- {title}{progress_badge} ---")
        for line in plan_text.splitlines():
            print(f"  {line}")
