"""Formatted Tool Result Cards for tool executions."""

from __future__ import annotations

import json
from typing import Any

from coderai.cli.diff_render import render_diff_preview
from coderai.cli.plan_render import render_plan_preview
from coderai.core.session import SessionMessage

try:
    from rich.panel import Panel
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Panel = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


def parse_tool_message(message: SessionMessage) -> tuple[str, str, bool, dict[str, Any] | None]:
    """Parse tool result message JSON payload into (tool_name, summary, is_ok, metadata)."""
    content = message.content or ""
    metadata: dict[str, Any] | None = None
    try:
        result = json.loads(content)
        name = str(result.get("name") or "tool")
        ok = result.get("ok") is not False
        if isinstance(result.get("metadata"), dict):
            metadata = result["metadata"]

        if not ok:
            err = str(result.get("error", "failed"))
            return name, f"failed: {err[:120]}", False, metadata

        output = result.get("output")
        if isinstance(output, str):
            first_line = output.splitlines()[0] if output.splitlines() else "(no output)"
            return name, first_line[:120], True, metadata
        return name, "completed", True, metadata
    except (ValueError, TypeError):
        return "tool", content[:120], True, metadata


def render_tool_card(console: Any | None, message: SessionMessage) -> None:
    """Render a formatted tool result card with status, diffs, and checklists."""
    name, summary_text, ok, metadata = parse_tool_message(message)
    bullet = "[bold green]✓[/]" if ok else "[bold red]✗[/]"

    if console is not None and _RICH:
        # Display main tool status line
        console.print(f"  {bullet} [bold cyan]{name}[/] [dim]•[/] [white]{summary_text}[/]")

        # Tool-specific rich cards
        if metadata:
            # Diff preview for Edit / Write
            diff_text = metadata.get("diff_preview")
            if isinstance(diff_text, str) and diff_text.strip():
                render_diff_preview(console, diff_text, title=f"{name} Changes")

            # Plan preview for UpdatePlan
            plan_text = metadata.get("plan")
            if name == "UpdatePlan" and isinstance(plan_text, str) and plan_text.strip():
                render_plan_preview(console, plan_text, title="Updated Plan")
    else:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}: {summary_text}")
        if metadata:
            diff_text = metadata.get("diff_preview")
            if isinstance(diff_text, str) and diff_text.strip():
                render_diff_preview(None, diff_text, title=f"{name} Changes")
            plan_text = metadata.get("plan")
            if name == "UpdatePlan" and isinstance(plan_text, str) and plan_text.strip():
                render_plan_preview(None, plan_text, title="Updated Plan")
