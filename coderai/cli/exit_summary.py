"""Session exit summary card rendering."""

from __future__ import annotations

import json
from typing import Any

from coderai.core.session import SessionEntry, SessionManager

try:
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Panel = None  # type: ignore[assignment,misc]
    Table = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


def compute_session_stats(mgr: SessionManager, session_id: str | None) -> dict[str, Any]:
    """Compute turn counts, modified files, and token usage for the active session."""
    stats: dict[str, Any] = {
        "session_id": session_id or "none",
        "turns": 0,
        "files_modified": [],
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "active_tokens": 0,
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
    if stats["turns"] == 0 and stats["total_tokens"] == 0 and not stats["files_modified"]:
        if console is not None and _RICH:
            console.print("\n[dim]Session closed. Happy coding with CoderAI![/]\n")
        else:
            print("\nSession closed. Happy coding with CoderAI!\n")
        return

    files_str = ", ".join(stats["files_modified"]) if stats["files_modified"] else "None"
    checkpoint_str = stats["checkpoint_hash"] or "clean"

    if console is not None and _RICH and Panel is not None and Table is not None:
        table = Table.grid(padding=(0, 2))
        table.add_column("Key", style="dim cyan", width=18)
        table.add_column("Value", style="bold white")

        table.add_row("Session ID:", stats["session_id"][:16])
        table.add_row("Conversation Turns:", f"{stats['turns']}")
        table.add_row("Files Modified:", f"[bold green]{files_str}[/]")
        table.add_row(
            "Token Usage:",
            f"Prompt: {stats['prompt_tokens']:,} | Completion: {stats['completion_tokens']:,} | Total: {stats['total_tokens']:,}",
        )
        table.add_row("Active Context:", f"{stats['active_tokens']:,} tokens")
        table.add_row("Checkpoint Hash:", f"[bold magenta]{checkpoint_str}[/]")

        panel = Panel(
            table,
            title="[bold cyan]CoderAI Session Summary[/]",
            border_style="bright_blue",
            padding=(0, 1),
        )
        console.print()
        console.print(panel)
        console.print("[dim]Goodbye! Your session checkpoints and history are saved.[/]\n")
    else:
        print("\n--- CoderAI Session Summary ---")
        print(f"  Session ID:         {stats['session_id'][:16]}")
        print(f"  Conversation Turns: {stats['turns']}")
        print(f"  Files Modified:     {files_str}")
        print(
            f"  Tokens (P/C/Total): {stats['prompt_tokens']} / {stats['completion_tokens']} / {stats['total_tokens']}"
        )
        print(f"  Active Context:     {stats['active_tokens']} tokens")
        print(f"  Checkpoint:         {checkpoint_str}")
        print("Goodbye! Your session is saved.\n")
