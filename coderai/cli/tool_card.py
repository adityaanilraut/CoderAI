"""Formatted tool-result cards for CLI tool executions."""

from __future__ import annotations

import json
from typing import Any


from coderai.cli.diff_render import render_diff_preview
from coderai.cli.plan_render import render_plan_preview
from coderai.core.session import SessionMessage

_RICH = True


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
    """Render a compact terminal command output event."""
    cmd = metadata.get("command") or ""
    exit_code = metadata.get("exit_code") if "exit_code" in metadata else (0 if ok else 1)
    status_style = "bold green" if ok else "bold red"
    status_text = f"exit {exit_code}" if exit_code is not None else ("ok" if ok else "failed")

    header_text = (
        f"    ↳ [bold cyan]$ {cmd}[/] [{status_style}]({status_text})[/]"
        if cmd
        else f"    ↳ [{status_style}]Shell Output ({status_text})[/]"
    )

    if console is not None and _RICH:
        console.print(header_text)
        content_lines: list[str] = []
        if output_text and output_text.strip():
            content_lines.extend(output_text.strip().splitlines())
        if error_text and error_text.strip():
            content_lines.extend(
                f"[bold red]Error:[/] {line}" for line in error_text.strip().splitlines()
            )

        if content_lines:
            limit = 20
            displayed = content_lines[:limit]
            for line in displayed:
                console.print(f"      [dim]│[/] {line}")
            if len(content_lines) > limit:
                console.print(
                    f"      [dim]... ({len(content_lines) - limit} more lines truncated)[/]"
                )
    else:
        plain_header = (
            f"    ↳ $ {cmd} ({status_text})" if cmd else f"    ↳ Shell Output ({status_text})"
        )
        print(plain_header)
        if output_text and output_text.strip():
            for line in output_text.strip().splitlines()[:20]:
                print(f"      | {line}")
        if error_text and error_text.strip():
            print(f"      Error: {error_text.strip()}")


def _render_search_card(console: Any, output_text: str | None, metadata: dict[str, Any]) -> None:
    """Render compact web search results matching deepseek-harness webCardModel presentation."""
    raw_results = metadata.get("results") or []
    sources: list[dict[str, Any]] = []
    queries: list[str] = []
    seen_urls: set[str] = set()

    for item in raw_results:
        if isinstance(item, dict):
            q = item.get("query")
            if q and q not in queries:
                queries.append(q)
            for src in item.get("sources") or []:
                if isinstance(src, dict) and src.get("url") and src["url"] not in seen_urls:
                    seen_urls.add(src["url"])
                    sources.append(src)
        elif isinstance(item, dict) and "url" in item and item.get("url"):
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                sources.append(item)

    if not sources and isinstance(metadata.get("sources"), list):
        for src in metadata["sources"]:
            if isinstance(src, dict) and src.get("url") and src["url"] not in seen_urls:
                seen_urls.add(src["url"])
                sources.append(src)

    query_title = metadata.get("query") or (", ".join(queries) if queries else "")

    if console is not None and _RICH:
        title = (
            f'    ↳ [bold cyan]Web Search:[/] [bold yellow]"{query_title}"[/]'
            if query_title
            else "    ↳ [bold cyan]Web Search Results[/]"
        )
        console.print(title)
        for idx, src in enumerate(sources[:6], 1):
            s_title = src.get("title") or src.get("url") or "Source"
            s_url = src.get("url") or ""
            snippet = src.get("snippet") or ""
            date_str = (
                f" [dim cyan]({src.get('publishedAt') or src.get('published_at')})[/]"
                if (src.get("publishedAt") or src.get("published_at"))
                else ""
            )
            console.print(
                f"      [bold cyan]{idx}.[/] [bold]{s_title}[/]{date_str} [dim]•[/] [dim cyan]{s_url}[/]"
            )
            if snippet:
                snip_short = snippet[:120] + "..." if len(snippet) > 120 else snippet
                console.print(f"         [dim]{snip_short}[/]")
    elif sources:
        print(f"    ↳ Web Search: {query_title or 'Results'}")
        for idx, src in enumerate(sources[:6], 1):
            print(f"      {idx}. {src.get('title') or src.get('url')} - {src.get('url')}")


def _render_fetch_card(
    console: Any,
    output_text: str | None,
    error_text: str | None,
    metadata: dict[str, Any],
    ok: bool,
) -> None:
    """Render a compact WebFetch result event."""
    url = metadata.get("url") or ""
    status_code = metadata.get("status_code") or metadata.get("status") or (200 if ok else 400)
    bytes_count = (
        metadata.get("bytes")
        or metadata.get("content_length")
        or (len(output_text.encode("utf-8")) if output_text else 0)
    )

    try:
        sc_num = int(status_code)
    except (ValueError, TypeError):
        sc_num = 200 if ok else 400

    status_style = "bold green" if (200 <= sc_num < 300) else "bold red"
    size_kb = bytes_count / 1024.0
    size_str = f"{size_kb:.1f} KB" if size_kb >= 1.0 else f"{bytes_count} B"

    if console is not None and _RICH:
        console.print(
            f"    ↳ [bold cyan]WebFetch[/] [{status_style}][{status_code}][/] [dim]({size_str})[/] • [dim]{url}[/]"
        )
        if output_text and output_text.strip():
            preview_lines = output_text.strip().splitlines()[:5]
            for pl in preview_lines:
                console.print(f"      [dim]│[/] {pl[:100]}")
    else:
        print(f"    ↳ WebFetch [{status_code}] ({size_str}) - {url}")


def _render_read_card(
    console: Any, file_path: str, metadata: dict[str, Any], output_text: str | None
) -> None:
    """Render a compact file slice inspection event."""
    snip_id = metadata.get("snippet_id")
    lines_cnt = metadata.get("line_count")
    offset = metadata.get("offset", 1)
    range_str = f"L{offset}-L{offset + lines_cnt - 1}" if lines_cnt else ""
    target_str = f"[bold cyan]{file_path}[/]" if file_path else "File Read"

    badges = []
    if range_str:
        badges.append(f"[bold yellow]{range_str}[/]")
    if lines_cnt:
        badges.append(f"[dim]{lines_cnt} lines[/]")
    if snip_id:
        badges.append(f"[bold magenta]snippet:{snip_id}[/]")

    badge_info = f" ({', '.join(badges)})" if badges else ""
    title = f"    ↳ {target_str}{badge_info}"

    if console is not None and _RICH:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()
            display_limit = 15
            for idx, line in enumerate(lines[:display_limit]):
                line_no = (offset or 1) + idx
                console.print(f"      [dim]{line_no:>4} │[/] {line}")
            if len(lines) > display_limit:
                console.print(
                    f"      [dim]... ({len(lines) - display_limit} more lines truncated)[/]"
                )
    else:
        print(
            f"    ↳ Read: {file_path} ({lines_cnt or len(output_text.splitlines()) if output_text else 0} lines)"
        )


def _render_search_grep_card(
    console: Any, output_text: str | None, metadata: dict[str, Any], ok: bool
) -> None:
    """Render grep / glob code search matches cleanly."""
    query = metadata.get("query") or metadata.get("pattern") or ""
    path = metadata.get("path") or metadata.get("directory") or ""
    matches_count = metadata.get("matches_count") or (
        len(output_text.splitlines()) if output_text else 0
    )

    title = f"    ↳ [bold cyan]Search:[/] [bold yellow]'{query}'[/]"
    if path:
        title += f" in [dim]{path}[/]"
    title += f" [dim]({matches_count} matches)[/]"

    if console is not None and _RICH:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()
            for line in lines[:15]:
                console.print(f"      [dim]│[/] {line}")
            if len(lines) > 15:
                console.print(f"      [dim]... ({len(lines) - 15} more matches truncated)[/]")
    elif output_text:
        print(f"    ↳ Search '{query}': {matches_count} matches")


def _render_lsp_card(
    console: Any, output_text: str | None, metadata: dict[str, Any], ok: bool
) -> None:
    """Render language server diagnostics cleanly."""
    diagnostics = metadata.get("diagnostics") or []
    file_path = metadata.get("file_path") or ""

    if console is not None and _RICH:
        diag_count = len(diagnostics) if diagnostics else (1 if output_text else 0)
        console.print(
            f"    ↳ [bold yellow]LSP Diagnostics:[/] [bold cyan]{file_path}[/] [dim]({diag_count} issues)[/]"
        )
        if diagnostics:
            for diag in diagnostics[:10]:
                sev = str(diag.get("severity", "error")).lower()
                sev_badge = (
                    "[bold red]ERROR[/]"
                    if "err" in sev
                    else ("[bold yellow]WARN[/]" if "warn" in sev else "[dim cyan]INFO[/]")
                )
                line_col = f"L{diag.get('line', 1)}:C{diag.get('col', 1)}"
                msg = diag.get("message", "")
                code = f" [dim]({diag.get('code')})[/]" if diag.get("code") else ""
                console.print(f"      {sev_badge} [dim cyan]{line_col}[/] {msg}{code}")
        elif output_text and output_text.strip():
            console.print(f"      {output_text.strip()}")
    elif output_text:
        print(f"    ↳ LSP: {file_path}\n      {output_text.strip()}")


def _render_subagent_card(
    console: Any, output_text: str | None, metadata: dict[str, Any], ok: bool
) -> None:
    """Render delegated subagent task execution cleanly."""
    task_name = metadata.get("task_name") or metadata.get("task") or "Subagent Task"
    agent_id = metadata.get("agent_id") or metadata.get("id") or ""
    status = "completed" if ok else "failed"
    status_style = "bold green" if ok else "bold red"

    title = f"    ↳ [bold magenta]Subagent:[/] [white]{task_name}[/] [{status_style}]({status})[/]"
    if agent_id:
        title += f" [dim]id:{agent_id[:8]}[/]"

    if console is not None and _RICH:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()[:10]
            for line in lines:
                console.print(f"      [dim]│[/] {line}")
    elif output_text:
        print(f"    ↳ Subagent {task_name}: {status}")


def render_tool_card(console: Any | None, message: SessionMessage) -> None:
    """Render a compact sequential tool result event with status, diffs, terminal outputs, and checklists."""
    name, summary_text, ok, metadata = parse_tool_message(message)
    bullet = "[bold green]●[/]" if ok else "[bold red]✗[/]"

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

        # Tool-specific compact events
        if metadata:
            file_path = metadata.get("file_path") or metadata.get("target_path") or ""

            # Diff preview for Edit / Write
            diff_text = metadata.get("diff_preview")
            if isinstance(diff_text, str) and diff_text.strip():
                card_title = f"{name}: {file_path}" if file_path else f"{name} Changes"
                render_diff_preview(console, diff_text, title=card_title)

            # Plan preview for UpdatePlan
            plan_text = metadata.get("plan")
            if (
                name in ("UpdatePlan", "update_plan", "write_plan")
                and isinstance(plan_text, str)
                and plan_text.strip()
            ):
                render_plan_preview(console, plan_text, title="Updated Plan")

            # Bash tool card
            if name in ("bash", "Bash", "terminal"):
                _render_bash_card(console, raw_output, raw_error, metadata, ok)

            # WebSearch tool card
            elif name in ("WebSearch", "web_search"):
                _render_search_card(console, raw_output, metadata)

            # WebFetch tool card
            elif name in ("WebFetch", "web_fetch", "fetch"):
                _render_fetch_card(console, raw_output, raw_error, metadata, ok)

            # Code search / grep / glob card
            elif name in ("grep", "glob", "file_search", "find_files"):
                _render_search_grep_card(console, raw_output, metadata, ok)

            # LSP diagnostics card
            elif name in ("lsp", "diagnostics", "typecheck"):
                _render_lsp_card(console, raw_output, metadata, ok)

            # Subagent task card
            elif name in ("subagent", "delegate", "agent_task", "invoke_agent"):
                _render_subagent_card(console, raw_output, metadata, ok)

            # Read tool snippet info
            elif name in ("read", "Read", "view_file"):
                _render_read_card(console, file_path, metadata, raw_output)
    else:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}: {summary_text}")
        if metadata:
            diff_text = metadata.get("diff_preview")
            if isinstance(diff_text, str) and diff_text.strip():
                render_diff_preview(None, diff_text, title=f"{name} Changes")
            plan_text = metadata.get("plan")
            if (
                name in ("UpdatePlan", "update_plan")
                and isinstance(plan_text, str)
                and plan_text.strip()
            ):
                render_plan_preview(None, plan_text, title="Updated Plan")
