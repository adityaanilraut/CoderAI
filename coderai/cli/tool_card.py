"""Formatted Tool Result Cards for tool executions."""

from __future__ import annotations

import json
from typing import Any

from coderai.cli.diff_render import render_diff_preview
from coderai.cli.plan_render import render_plan_preview
from coderai.core.session import SessionMessage

try:
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Panel = None  # type: ignore[assignment,misc]
    Syntax = None  # type: ignore[assignment,misc]
    Table = None  # type: ignore[assignment,misc]
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


def _render_bash_card(
    console: Any,
    output_text: str | None,
    error_text: str | None,
    metadata: dict[str, Any],
    ok: bool,
) -> None:
    """Render a dedicated terminal command output card."""
    cmd = metadata.get("command") or ""
    exit_code = metadata.get("exit_code") if "exit_code" in metadata else (0 if ok else 1)
    status_style = "bold green" if ok else "bold red"
    status_text = f"exit {exit_code}" if exit_code is not None else ("ok" if ok else "failed")

    title = (
        f"[bold cyan]$ {cmd}[/] [{status_style}]({status_text})[/]"
        if cmd
        else f"[{status_style}]Shell Output ({status_text})[/]"
    )
    content_lines: list[str] = []
    if output_text and output_text.strip():
        content_lines.append(output_text.strip())
    if error_text and error_text.strip():
        content_lines.append(f"[bold red]Error:[/] {error_text.strip()}")

    body = "\n".join(content_lines)
    if body and len(body.splitlines()) > 1:
        if Panel is not None:
            panel = Panel(
                body[:1500] + ("\n... [dim](output truncated)[/]" if len(body) > 1500 else ""),
                title=title,
                border_style="cyan" if ok else "red",
                padding=(0, 1),
            )
            console.print(panel)
        else:
            console.print(f"  {title}\n{body}")


def _render_search_card(console: Any, output_text: str | None, metadata: dict[str, Any]) -> None:
    """Render formatted web search results."""
    query = metadata.get("query") or ""
    results = metadata.get("results") or []

    if results and Table is not None:
        table = Table(title=f"Web Search: {query}", border_style="cyan", padding=(0, 1))
        table.add_column("#", style="bold cyan", width=3)
        table.add_column("Title & URL", style="white")
        table.add_column("Snippet", style="dim")

        for idx, res in enumerate(results[:5], 1):
            title = res.get("title", "")
            url = res.get("url", "")
            snippet = res.get("snippet", "")
            table.add_row(
                str(idx),
                f"[bold]{title}[/]\n[dim cyan]{url}[/]",
                snippet[:120] + "..." if len(snippet) > 120 else snippet,
            )
        console.print(table)


def render_tool_card(console: Any | None, message: SessionMessage) -> None:
    """Render a formatted tool result card with status, diffs, terminal outputs, and checklists."""
    name, summary_text, ok, metadata = parse_tool_message(message)
    bullet = "[bold green]✓[/]" if ok else "[bold red]✗[/]"

    raw_output: str | None = None
    raw_error: str | None = None
    try:
        parsed_payload = json.loads(message.content or "{}")
        raw_output = parsed_payload.get("output")
        raw_error = parsed_payload.get("error")
    except Exception:
        pass

    if console is not None and _RICH:
        # Display main tool status line
        console.print(f"  {bullet} [bold cyan]{name}[/] [dim]•[/] [white]{summary_text}[/]")

        # Tool-specific rich cards
        if metadata:
            file_path = metadata.get("file_path") or metadata.get("target_path") or ""

            # Diff preview for Edit / Write
            diff_text = metadata.get("diff_preview")
            if isinstance(diff_text, str) and diff_text.strip():
                card_title = f"{name}: {file_path}" if file_path else f"{name} Changes"
                render_diff_preview(console, diff_text, title=card_title)

            # Plan preview for UpdatePlan
            plan_text = metadata.get("plan")
            if name == "UpdatePlan" and isinstance(plan_text, str) and plan_text.strip():
                render_plan_preview(console, plan_text, title="Updated Plan")

            # Bash tool card
            if name == "bash":
                _render_bash_card(console, raw_output, raw_error, metadata, ok)

            # WebSearch tool card
            if name == "WebSearch":
                _render_search_card(console, raw_output, metadata)

            # Read tool snippet info
            if name == "read":
                snip_id = metadata.get("snippet_id")
                lines_cnt = metadata.get("line_count")
                offset = metadata.get("offset", 1)
                range_str = f"L{offset}-L{offset + lines_cnt - 1}" if lines_cnt else ""
                target_str = f" [cyan]{file_path}[/]" if file_path else ""
                details = []
                if range_str:
                    details.append(range_str)
                if lines_cnt:
                    details.append(f"{lines_cnt} lines")
                if snip_id:
                    details.append(f"anchored [bold cyan]snippet:{snip_id}[/]")
                if details:
                    console.print(f"    [dim]↳{target_str} ({', '.join(details)})[/]")
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
