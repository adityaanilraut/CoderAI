# Ported from coderai/cli/web_cmd.py - kimi structure (cli/web.py).
"""Kimi ``/web`` + ``/vis`` parity: hand off the live session to a browser UI.

Phase 1: export the session to a Markdown/JSON snapshot and open it locally,
plus print the (future) server address. A full FastAPI server is tracked as
Phase 3 work; these commands establish the UX contract now.
"""

from __future__ import annotations

import os
import tempfile
import webbrowser
from typing import Any

WEB_PORT = 5494
VIS_PORT = 5495


def _snapshot_session(mgr: Any, session_id: str | None) -> str:
    from coderai.utils.export import export_session_to_markdown

    if not session_id:
        return ""
    try:
        out = os.path.join(tempfile.gettempdir(), f"coderai-web-{session_id[:8]}.md")
        return export_session_to_markdown(mgr, session_id, out)
    except Exception:
        return ""


def cmd_web(console: Any, mgr: Any, session_id: str | None, arg: str = "") -> str:
    """Open current session snapshot in the browser (``/web``)."""
    port = WEB_PORT
    arg = (arg or "").strip()
    if arg.isdigit():
        port = int(arg)
    snapshot = _snapshot_session(mgr, session_id)
    url = f"http://127.0.0.1:{port}/sessions/{session_id or 'new'}"
    msg = f"Web UI (preview): session snapshot at {snapshot or '(none)'}\nOpen: {url}"
    try:
        if snapshot:
            webbrowser.open(f"file://{snapshot}")
        else:
            webbrowser.open(url)
    except Exception:
        pass
    if console is not None:
        try:
            console.print(f"[bold cyan]{msg}[/]")
        except Exception:
            print(msg)
    else:
        print(msg)
    try:
        from coderai.ui.shell.prompt import toast

        toast("Web server not yet embedded; opened snapshot preview.", topic="web")
    except Exception:
        pass
    return url


def cmd_vis(console: Any, mgr: Any, session_id: str | None, arg: str = "") -> str:
    """Open tracing visualizer (``/vis``): token/turn timeline for the session."""
    url = f"http://127.0.0.1:{VIS_PORT}/trace/{session_id or 'new'}"
    try:
        entry = mgr.get_session(session_id) if session_id else None
        msgs = getattr(entry, "messages", []) if entry else []
        roles: dict[str, int] = {}
        for m in msgs:
            roles[str(getattr(m, 'role', '?'))] += 1
    except Exception:
        roles = {}
    line = f"Trace: {len(roles) and roles} turns — open: {url}"
    if console is not None:
        try:
            console.print(f"[bold cyan]{line}[/]")
        except Exception:
            print(line)
    else:
        print(line)
    # Reuse snapshot for now.
    snapshot = _snapshot_session(mgr, session_id)
    try:
        if snapshot:
            webbrowser.open(f"file://{snapshot}")
    except Exception:
        pass
    return url
