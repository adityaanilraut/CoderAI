"""Tool-result summaries and terminal cards."""

from __future__ import annotations

import json
from typing import Any

from rich.markup import escape

from coderai.soul.session.models import SessionMessage
from coderai.utils.rich.diff_render import render_diff_preview

from ._todos import render_todo_list


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


def _render_collapsible_block(
    console: Any,
    lines: list[str],
    preview_limit: int = 8,
    indent: str = "      [dim]│[/] ",
) -> None:
    """Render lines with collapsible preview (changed-lines-only style).

    Shows first preview_limit lines + hint for remaining.
    """
    if not lines:
        return
    shown = lines[:preview_limit]
    for line in shown:
        # UI-A2: untrusted tool output is never markup. A line that merely
        # looks like "[...]" must not be rendered as a style tag.
        console.print(f"{indent}{escape(line)}")
    remaining = len(lines) - len(shown)
    if remaining > 0:
        console.print(f"      [dim italic]... {remaining} more lines (press Enter to expand)[/]")


def _render_bash_card(
    console: Any,
    output_text: str | None,
    error_text: str | None,
    metadata: dict[str, Any],
    ok: bool,
) -> None:
    """Render a compact terminal command output event with collapsible streaming."""
    cmd = metadata.get("command") or ""
    exit_code = metadata.get("exit_code") if "exit_code" in metadata else (0 if ok else 1)
    status_style = "bold green" if ok else "bold red"
    status_text = f"exit {exit_code}" if exit_code is not None else ("ok" if ok else "failed")
    elapsed = metadata.get("duration_ms") or metadata.get("elapsed_ms")
    elapsed_str = f" [dim]({elapsed:.0f}ms)[/]" if isinstance(elapsed, (int, float)) else ""

    header_text = (
        f"    ↳ [bold cyan]$ {escape(cmd)}[/] [{status_style}]({status_text})[/]{elapsed_str}"
        if cmd
        else f"    ↳ [{status_style}]Shell Output ({status_text})[/]{elapsed_str}"
    )

    if console is not None:
        # Grouped block with side border

        console.print(header_text)
        content_lines: list[str] = []
        if output_text and output_text.strip():
            content_lines.extend(output_text.strip().splitlines())
        if error_text and error_text.strip():
            content_lines.extend(
                f"[bold red]Error:[/] {escape(line)}" for line in error_text.strip().splitlines()
            )

        if content_lines:
            _render_collapsible_block(console, content_lines, preview_limit=12)
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

    if console is not None:
        title = (
            f'    ↳ [bold cyan]Web Search:[/] [bold yellow]"{escape(query_title)}"[/]'
            if query_title
            else "    ↳ [bold cyan]Web Search Results[/]"
        )
        console.print(title)
        for idx, src in enumerate(sources[:6], 1):
            s_title = src.get("title") or src.get("url") or "Source"
            s_url = src.get("url") or ""
            snippet = src.get("snippet") or ""
            date_str = (
                f" [dim cyan]({escape(str(src.get('publishedAt') or src.get('published_at')))})[/]"
                if (src.get("publishedAt") or src.get("published_at"))
                else ""
            )
            console.print(
                f"      [bold cyan]{idx}.[/] [bold]{escape(str(s_title))}[/]{date_str} [dim]•[/] [dim cyan]{escape(str(s_url))}[/]"
            )
            if snippet:
                snip_short = snippet[:120] + "..." if len(snippet) > 120 else snippet
                console.print(f"         [dim]{escape(snip_short)}[/]")
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

    if console is not None:
        console.print(
            f"    ↳ [bold cyan]WebFetch[/] [{status_style}][{escape(str(status_code))}][/] [dim]({escape(size_str)})[/] • [dim]{escape(str(url))}[/]"
        )
        if output_text and output_text.strip():
            preview_lines = output_text.strip().splitlines()[:5]
            for pl in preview_lines:
                console.print(f"      [dim]│[/] {escape(pl[:100])}")
    else:
        print(f"    ↳ WebFetch [{status_code}] ({size_str}) - {url}")


def _render_read_card(
    console: Any, file_path: str, metadata: dict[str, Any], output_text: str | None
) -> None:
    """Render a compact file slice inspection event with syntax highlighting."""
    snip_id = metadata.get("snippet_id")
    lines_cnt = metadata.get("line_count")
    offset = metadata.get("offset", 1)
    range_str = f"L{offset}-L{offset + lines_cnt - 1}" if lines_cnt else ""
    target_str = f"[bold cyan]{escape(str(file_path))}[/]" if file_path else "File Read"

    badges = []
    if range_str:
        badges.append(f"[bold yellow]{range_str}[/]")
    if lines_cnt:
        badges.append(f"[dim]{lines_cnt} lines[/]")
    if snip_id:
        badges.append(f"[bold magenta]snippet:{snip_id}[/]")

    badge_info = f" ({', '.join(badges)})" if badges else ""
    title = f"    ↳ {target_str}{badge_info}"

    if console is not None:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()
            display_limit = 15
            # Try syntax highlighting per file extension
            try:
                from coderai.utils.rich.syntax import CoderAISyntax

                ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "text"
                # Highlight as a block for better token colors
                code = "\n".join(lines[:display_limit])
                syntax = CoderAISyntax(
                    code, ext, theme="coderai-ansi", line_numbers=True, start_line=offset or 1
                )
                console.print(syntax)
            except Exception:
                for idx, line in enumerate(lines[:display_limit]):
                    line_no = (offset or 1) + idx
                    console.print(f"      [dim]{line_no:>4} │[/] {escape(line)}")
            if len(lines) > display_limit:
                console.print(
                    f"      [dim italic]... ({len(lines) - display_limit} more lines hidden — press Enter to expand)[/]"
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
    matches_count = metadata.get("matches_count", metadata.get("count"))
    if matches_count is None:
        matches_count = len(output_text.splitlines()) if output_text else 0

    title = f"    ↳ [bold cyan]Search:[/] [bold yellow]'{escape(query)}'[/]"
    if path:
        title += f" in [dim]{escape(path)}[/]"
    title += f" [dim]({matches_count} matches)[/]"

    if console is not None:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()
            for line in lines[:15]:
                console.print(f"      [dim]│[/] {escape(line)}")
            if len(lines) > 15:
                console.print(f"      [dim]... ({len(lines) - 15} more matches hidden)[/]")
    elif output_text:
        print(f"    ↳ Search '{query}': {matches_count} matches")


def _render_subagent_card(
    console: Any, output_text: str | None, metadata: dict[str, Any], ok: bool
) -> None:
    """Render delegated subagent task execution cleanly."""
    task_name = metadata.get("task_name") or metadata.get("task") or "Subagent Task"
    agent_id = metadata.get("agent_id") or metadata.get("id") or ""
    status = "completed" if ok else "failed"
    status_style = "bold green" if ok else "bold red"

    title = f"    ↳ [bold magenta]Subagent:[/] [white]{escape(task_name)}[/] [{status_style}]({status})[/]"
    if agent_id:
        title += f" [dim]id:{agent_id[:8]}[/]"

    if console is not None:
        console.print(title)
        if output_text and output_text.strip():
            lines = output_text.strip().splitlines()[:10]
            for line in lines:
                console.print(f"      [dim]│[/] {escape(line)}")
    elif output_text:
        print(f"    ↳ Subagent {task_name}: {status}")


def _render_session_card(
    console: Any, output_text: str | None, metadata: dict[str, Any], ok: bool
) -> None:
    """Render session query / trace results cleanly."""
    query = metadata.get("query") or metadata.get("session_id") or ""
    results_count = metadata.get("count") or (len(output_text.splitlines()) if output_text else 0)
    status_style = "bold green" if ok else "bold red"

    title = (
        f"    ↳ [bold cyan]Session Query:[/] [bold yellow]'{escape(query)}'[/] [{status_style}]({results_count} events)[/]"
        if query
        else f"    ↳ [bold cyan]Session Query[/] [{status_style}]({results_count} events)[/]"
    )

    if console is not None:
        console.print(title)
        if output_text and output_text.strip():
            for line in output_text.strip().splitlines()[:10]:
                console.print(f"      [dim]│[/] {escape(line)}")
    elif output_text:
        print(f"    ↳ Session Query: {results_count} events")


def render_tool_card(console: Any | None, message: SessionMessage) -> None:
    """Render a compact sequential tool result event with grouped blocks, collapsible output, and status."""
    name, summary_text, ok, metadata = parse_tool_message(message)
    # Bullet: green dot ok, dark_red error, with spinner semantics
    bullet = "[bold green]●[/]" if ok else "[bold red]✗[/]"

    raw_output: str | None = None
    raw_error: str | None = None
    try:
        parsed_payload = json.loads(message.content or "{}")
        raw_output = parsed_payload.get("output")
        raw_error = parsed_payload.get("error")
    except Exception:
        pass

    if console is not None:
        # Display main tool status line (name can come from an MCP server).
        console.print(
            f"  {bullet} [bold cyan]{escape(str(name))}[/] [dim]•[/] [white]{escape(summary_text)}[/]"
        )

        # Tool-specific compact events
        if metadata:
            file_path = metadata.get("file_path") or metadata.get("target_path") or ""

            # Diff preview for Edit / Write
            diff_text = metadata.get("diff_preview")
            if isinstance(diff_text, str) and diff_text.strip():
                card_title = f"{name}: {file_path}" if file_path else f"{name} Changes"
                render_diff_preview(console, diff_text, title=card_title)

            # Plan / Todo preview for UpdatePlan / todo_write / SetTodoList
            todos_data = metadata.get("todos")
            plan_text = metadata.get("plan")
            if name in (
                "todo_write",
                "SetTodoList",
                "set_todo_list",
                "UpdatePlan",
                "update_plan",
                "write_plan",
            ):
                if todos_data and isinstance(todos_data, list):
                    render_todo_list(console, todos_data, title="Todo")
                elif isinstance(plan_text, str) and plan_text.strip():
                    render_todo_list(console, plan_text, title="Todo")

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

            # Subagent task card
            elif name in ("subagent", "delegate", "agent_task", "invoke_agent"):
                _render_subagent_card(console, raw_output, metadata, ok)

            # Session query / trace card
            elif name in (
                "session_query",
                "session_search",
                "session_trace",
                "session_event_search",
                "session_event_read",
            ):
                _render_session_card(console, raw_output, metadata, ok)

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
            todos_data = metadata.get("todos")
            plan_text = metadata.get("plan")
            if name in (
                "todo_write",
                "SetTodoList",
                "set_todo_list",
                "UpdatePlan",
                "update_plan",
                "write_plan",
            ):
                if todos_data and isinstance(todos_data, list):
                    render_todo_list(None, todos_data, title="Todo")
                elif isinstance(plan_text, str) and plan_text.strip():
                    render_todo_list(None, plan_text, title="Todo")
