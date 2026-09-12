from __future__ import annotations

from typing import Any

def cmd_debug(mgr: Any, session_id: str | None, console: Any = None) -> dict[str, Any]:
    """Show context debug info: message/token counts, checkpoints, history (``/debug``)."""
    info: dict[str, Any] = {
        "session_id": session_id,
        "messages": 0,
        "tokens": 0,
        "checkpoints": 0,
        "turns": 0,
    }
    history: list[str] = []
    try:
        entry = mgr.get_session(session_id) if session_id else None
        if entry is not None:
            msgs = getattr(entry, "messages", []) or []
            info["messages"] = len(msgs)
            info["tokens"] = getattr(entry, "active_tokens", 0)
            info["turns"] = getattr(entry, "turn_count", 0) or len(msgs)
            hist = getattr(entry, "history", None)
            if isinstance(hist, list):
                info["checkpoints"] = len(hist)
            for i, m in enumerate(msgs[-20:]):
                role = getattr(m, "role", "?")
                content = str(getattr(m, "content", ""))[:120].replace("\n", " ")
                history.append(f"{i}: [{role}] {content}")
    except Exception:
        pass
    if console is not None:
        try:
            from rich.panel import Panel
            from rich.table import Table

            t = Table.grid(padding=(0, 2))
            t.add_column("Key", style="dim cyan", width=14)
            t.add_column("Value", style="bold white")
            for k in ("session_id", "messages", "tokens", "turns", "checkpoints"):
                t.add_row(f"{k}:", str(info[k]))
            console.print(Panel(t, title="[bold cyan]Debug[/]", border_style="blue"))
            if history:
                console.print("[dim]Last messages:[/]")
                for h in history[-10:]:
                    console.print(f"  [dim]{h}[/]")
        except Exception:
            print(info)
    else:
        print(info)
        for h in history[-10:]:
            print(" ", h)
    return info
