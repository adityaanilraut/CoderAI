"""Plan checklist and Todo list renderer for Plan Mode, UpdatePlan, and todo_write tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.text import Text


@dataclass
class TodoItem:
    title: str
    status: str = "pending"  # "pending", "in_progress", "completed", "cancelled"


@dataclass
class TodoDisplayBlock:
    items: list[TodoItem]
    title: str = "Todo"


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


def format_todo_item(title: str, status: str = "pending", indent: str = "") -> Text:
    """Format an individual todo item with modern icons and styling."""
    t = Text()
    normalized = status.strip().lower().replace(" ", "_")
    if normalized in ("in_progress", "active", "doing"):
        t.append(f"{indent}● ", style="bold #38bdf8")
        t.append(f"{title}\n", style="bold white")
    elif normalized in ("completed", "done", "finished"):
        t.append(f"{indent}✓ ", style="bold #4ade80")
        t.append(f"{title}\n", style="dim green")
    elif normalized in ("cancelled", "canceled", "skipped"):
        t.append(f"{indent}- ", style="dim yellow")
        t.append(f"{title}\n", style="dim yellow")
    else:  # pending
        t.append(f"{indent}○ ", style="bold #64748b")
        t.append(f"{title}\n", style="white")
    return t


def format_todo_content(todos: list[dict[str, Any]] | list[TodoItem] | str) -> Text:
    """Format structured todos or markdown checklist into styled Rich Text."""
    if isinstance(todos, str):
        return format_plan_content(todos)

    formatted = Text()
    for item in todos:
        if isinstance(item, TodoItem):
            title = item.title
            status = item.status
        elif isinstance(item, dict):
            title = str(item.get("content") or item.get("title") or "").strip()
            status = str(item.get("status") or "pending")
        else:
            continue
        if not title:
            continue
        formatted.append(format_todo_item(title, status))
    return formatted


def format_plan_content(plan_text: str) -> Text:
    """Format markdown checklist into styled Rich Text while preserving nested hierarchy."""
    formatted = Text()
    lines = plan_text.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            formatted.append("\n")
            continue

        # Calculate indentation depth (preserving sub-task nesting)
        leading_spaces = len(line) - len(line.lstrip(" "))
        indent_depth = max(0, leading_spaces // 2)
        base_indent = "  " * indent_depth

        if stripped.startswith(("- [x]", "* [x]", "- [X]", "* [X]")):
            item_text = stripped[5:].strip()
            formatted.append(format_todo_item(item_text, "completed", indent=base_indent))
        elif stripped.startswith(("- [>]", "* [>]", "- [*]", "* [*]")):
            item_text = stripped[5:].strip()
            formatted.append(format_todo_item(item_text, "in_progress", indent=base_indent))
        elif stripped.startswith(("- [-]", "* [-]")):
            item_text = stripped[5:].strip()
            formatted.append(format_todo_item(item_text, "cancelled", indent=base_indent))
        elif stripped.startswith(("- [ ]", "* [ ]")):
            item_text = stripped[5:].strip()
            formatted.append(format_todo_item(item_text, "pending", indent=base_indent))
        elif stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            formatted.append(f"\n{base_indent}{heading}\n", style="bold #38bdf8")
        elif stripped.startswith(("-", "*", "•")):
            item_text = stripped.lstrip("-*• ").strip()
            formatted.append(f"{base_indent}• ", style="dim #64748b")
            formatted.append(f"{item_text}\n", style="dim")
        else:
            formatted.append(f"{base_indent}{stripped}\n", style="dim")

    return formatted


def create_todo_block(
    todos: list[dict[str, Any]] | list[TodoItem] | str,
    title: str = "Todo",
    term_width: int = 80,
) -> RenderableType:
    """Create a self-contained Rich Renderable for the Todo list with sleek rules and styling."""
    content = format_todo_content(todos)
    if not str(content).strip():
        return Text("")

    header_text = Text()
    header_text.append(title, style="bold #38bdf8")

    elements: list[RenderableType] = [
        header_text,
        content,
    ]
    return Group(*elements)


def render_todo_list(
    console: Any | None,
    todos: list[dict[str, Any]] | list[TodoItem] | str,
    title: str = "Todo",
) -> None:
    """Render the gold-standard Todo list directly to the terminal."""
    active_console = console or Console()
    width = getattr(active_console, "width", 80) or 80
    block = create_todo_block(todos, title=title, term_width=width)
    active_console.print(block)


def render_plan_preview(
    console: Any | None,
    plan_text: str,
    title: str = "Todo",
) -> None:
    """Render the plan progress checklist as a clean gold-standard Todo event."""
    if not plan_text.strip():
        return
    render_todo_list(console, plan_text, title=title)
