"""Session exit summary card rendering."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.markup import escape

from coderai.soul.session.manager import SessionManager
from coderai.ui.shell.console import PANEL_BORDER_STYLE, PANEL_PADDING, kv_table
from coderai.soul.session.models import SessionEntry


def compute_session_stats(mgr: SessionManager, session_id: str | None) -> dict[str, Any]:
    """Compute turn counts, modified files, and token usage for the active session."""
    stats: dict[str, Any] = {
        "session_id": session_id or "none",
        "model": mgr.get_active_model(),
        "turns": 0,
        "files_modified": [],
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_hit_rate": 0.0,
        "active_tokens": 0,
        "estimated_cost": 0.0,
        "checkpoint_hash": None,
        "summary": "",
    }

    if not session_id:
        return stats

    entry: SessionEntry | None = mgr.get_session(session_id)
    if entry:
        stats["summary"] = entry.summary
        stats["active_tokens"] = entry.active_tokens
        if entry.usage:
            stats["prompt_tokens"] = entry.usage.get("prompt_tokens", 0)
            stats["completion_tokens"] = entry.usage.get("completion_tokens", 0)
            stats["total_tokens"] = entry.usage.get("total_tokens", 0)
            stats["cached_tokens"] = entry.usage.get("cached_tokens", 0)
            if stats["prompt_tokens"] > 0 and stats["cached_tokens"] > 0:
                stats["cache_hit_rate"] = round(
                    (stats["cached_tokens"] / stats["prompt_tokens"]) * 100.0, 1
                )

    # Count user turns and identify modified files from tool messages
    messages = mgr.list_session_messages(session_id)
    turns = 0
    modified_files: set[str] = set()

    for m in messages:
        if m.role == "user" and m.content:
            turns += 1
        elif m.role == "tool" and m.content:
            try:
                payload = json.loads(m.content)
                name = payload.get("name")
                if name in ("edit", "write") and payload.get("ok"):
                    meta = payload.get("metadata") or {}
                    path = meta.get("file_path") or meta.get("target_path")
                    if path:
                        modified_files.add(str(path))
            except Exception:
                pass

    stats["turns"] = turns
    stats["files_modified"] = sorted(modified_files)

    from coderai.ui.shell.runtime_view import session_cost

    stats["estimated_cost"] = session_cost(entry, stats["model"]) if entry else None

    # Checkpoint hash from git file history if available
    try:
        cur_ref = mgr.file_history.get_current_checkpoint_hash(session_id)
        if cur_ref:
            stats["checkpoint_hash"] = cur_ref[:10]
    except Exception:
        pass

    return stats


def render_exit_summary(console: Any | None, mgr: SessionManager, session_id: str | None) -> None:
    """Render the clean session exit summary card."""
    stats = compute_session_stats(mgr, session_id)
    active_console = console or Console()
    if stats["turns"] == 0 and stats["total_tokens"] == 0 and not stats["files_modified"]:
        active_console.print("\n[dim]Session closed. Happy coding with CoderAI![/]\n")
        return

    files_cnt = len(stats["files_modified"])
    files_str = (
        f"{files_cnt} files ({', '.join(stats['files_modified'])})" if files_cnt > 0 else "None"
    )
    checkpoint_str = stats["checkpoint_hash"] or "clean"
    from coderai.ui.shell.runtime_view import cost_text

    cost_str = cost_text(stats["estimated_cost"])

    rows: list[tuple[str, str]] = [
        ("Session ID:", f"[cyan]{escape(stats['session_id'])}[/]"),
        ("Active Model:", f"[bold cyan]{escape(stats['model'])}[/]"),
        ("Conversation Turns:", f"{stats['turns']}"),
        ("Files Modified:", f"[bold green]{escape(files_str)}[/]"),
    ]
    token_usage_str = f"Prompt: {stats['prompt_tokens']:,} | Comp: {stats['completion_tokens']:,} | Total: {stats['total_tokens']:,}"
    if stats.get("cached_tokens", 0) > 0:
        hit_rate = stats.get("cache_hit_rate", 0.0) or (
            (stats["cached_tokens"] / stats["prompt_tokens"] * 100.0)
            if stats["prompt_tokens"] > 0
            else 0.0
        )
        token_usage_str += f" | Cached: {stats['cached_tokens']:,} ({hit_rate:.1f}% hit)"
    rows.append(("Token Usage:", token_usage_str))
    rows.append(("Estimated Cost:", f"[bold green]{cost_str}[/]"))
    rows.append(("Active Context:", f"{stats['active_tokens']:,} tokens"))
    rows.append(("Checkpoint Hash:", f"[bold magenta]{checkpoint_str}[/]"))
    table = kv_table(rows)

    panel = Panel(
        table,
        title="[bold cyan]CoderAI Session Summary[/]",
        border_style=PANEL_BORDER_STYLE,
        padding=PANEL_PADDING,
    )
    active_console.print()
    active_console.print(panel)
    active_console.print(
        "[dim]Resume with coderai --last. /history inspects messages; /undo selects a checkpoint.[/]\n"
    )
