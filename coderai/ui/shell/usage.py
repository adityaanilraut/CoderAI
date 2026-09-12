from __future__ import annotations

from typing import Any

def cmd_usage(mgr: Any, session_id: str | None, console: Any = None) -> dict[str, Any]:
    """Show API usage/quota with progress bars.

    Uses local token accounting; quota endpoints are provider-specific so this
    reports consumption against the configured context window.
    """
    from coderai.config import get_default_context_window

    data: dict[str, Any] = {
        "active_tokens": 0,
        "max_tokens": 0,
        "pct": 0.0,
        "model": "",
    }
    try:
        entry = mgr.get_session(session_id) if session_id else None
        model = mgr.get_active_model() if hasattr(mgr, "get_active_model") else ""
        data["model"] = model
        max_ctx = get_default_context_window(model)
        active = getattr(entry, "active_tokens", 0) if entry else 0
        data["active_tokens"] = active
        data["max_tokens"] = max_ctx
        data["pct"] = (active / max_ctx * 100) if max_ctx > 0 else 0.0
    except Exception:
        pass
    bar_len = 20
    filled = max(0, min(bar_len, int(data["pct"] / 100 * bar_len)))
    bar = "█" * filled + "░" * (bar_len - filled)
    line = (
        f"Usage: {data['active_tokens']:,} / {data['max_tokens']:,} "
        f"({data['pct']:.1f}%) [{bar}] model={data['model']}"
    )
    if console is not None:
        try:
            from rich.panel import Panel

            console.print(Panel(f"[bold white]{line}[/]", title="[bold cyan]Usage[/]"))
        except Exception:
            print(line)
    else:
        print(line)
    return data
