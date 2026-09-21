"""Interactive background-task browser.

Three-column TUI: task list | detail | output preview.
Keys: Enter/O full output in pager, S stop (confirm), Tab filter,
R refresh, Q/Esc exit. Auto-refreshes every second.
Falls back to a plain table when Rich/prompt_toolkit is unavailable.
"""

from __future__ import annotations

import time
from typing import Any


def _collect_jobs(mgr: Any, session_id: str | None, active_only: bool = False) -> list[Any]:
    store = getattr(mgr, "job_store", None)
    if not store:
        return []
    jobs = list(getattr(store, "_jobs", {}).values())
    if session_id:
        jobs = [j for j in jobs if getattr(j, "session_id", None) in (session_id, "default", None)]
    if active_only:
        jobs = [j for j in jobs if getattr(j, "status", "") == "running"]
    return jobs


def _job_detail(job: Any) -> list[tuple[str, str]]:
    return [
        ("ID:", str(getattr(job, "id", "?"))),
        ("Status:", str(getattr(job, "status", "?"))),
        ("Kind:", str(getattr(job, "kind", "?"))),
        ("Label:", str(getattr(job, "label", ""))[:80]),
        ("Session:", str(getattr(job, "session_id", ""))),
        ("Exit code:", str(getattr(job, "exit_code", ""))),
    ]


def _job_preview(job: Any, n: int = 12) -> str:
    import os

    path = getattr(job, "output_path", None)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-n:]) or "(no output yet)"
        except Exception as e:
            return f"(cannot read output: {e})"
    return "(no output file)"


def run_task_browser(console: Any, mgr: Any, session_id: str | None) -> None:
    """Open the interactive task browser (blocking)."""
    try:
        from rich.live import Live
        from rich.panel import Panel
        from rich.table import Table
        from rich.columns import Columns
    except Exception:
        jobs = _collect_jobs(mgr, session_id)
        if not jobs:
            print("No background tasks.")
            return
        for j in jobs:
            print(
                f"[{getattr(j, 'status', '?')}] {getattr(j, 'id', '?')} {getattr(j, 'label', '')[:60]}"
            )
        return

    import sys
    import select as _select

    idx = 0
    active_only = False
    store = getattr(mgr, "job_store", None)

    def _render():
        jobs = _collect_jobs(mgr, session_id, active_only)
        left = Table(title="Tasks", border_style="cyan", width=38)
        left.add_column("#", width=3)
        left.add_column("Status", width=9)
        left.add_column("Label")
        for i, j in enumerate(jobs[:20]):
            marker = ">" if i == idx else " "
            status = str(getattr(j, "status", "?")).upper()
            left.add_row(
                f"{marker}{i}",
                status,
                str(getattr(j, "label", ""))[:30],
            )
        if jobs and 0 <= idx < len(jobs):
            job = jobs[idx]
            detail = Table(title="Detail", border_style="green", width=34)
            detail.add_column("K", width=10)
            detail.add_column("V")
            for k, v in _job_detail(job):
                detail.add_row(k, v)
            preview = Panel(
                _job_preview(job)[:1500],
                title="Output preview",
                border_style="yellow",
            )
        else:
            detail = Panel("(no tasks)", title="Detail")
            preview = Panel("", title="Output preview")
        footer = "[Enter/O] output  [S] stop  [Tab] filter(all/active)  [R] refresh  [Q] exit"
        return Columns([left, detail, preview]), footer, jobs

    if not sys.stdin.isatty():
        jobs = _collect_jobs(mgr, session_id)
        for j in jobs:
            console.print(
                f"[{getattr(j, 'status', '?')}] {getattr(j, 'id', '?')}"
            ) if console else print(j)
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        with Live(console=console, refresh_per_second=1, transient=True) as live:
            while True:
                table, footer, jobs = _render()
                live.update(table)
                # Non-blocking key read with 1s timeout (auto-refresh).
                r, _, _ = _select.select([sys.stdin], [], [], 1.0)
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if ch in ("q", "Q", "\x1b"):
                    break
                elif ch == "\t":
                    active_only = not active_only
                    idx = 0
                elif ch in ("r", "R"):
                    continue
                elif ch in ("\r", "\n", "o", "O"):
                    if jobs and 0 <= idx < len(jobs):
                        live.stop()
                        print(_job_preview(jobs[idx], n=200))
                        input("Press Enter to return...")
                        # re-enter live
                        return run_task_browser(console, mgr, session_id)
                elif ch in ("s", "S"):
                    if jobs and 0 <= idx < len(jobs) and store:
                        live.stop()
                        ans = (
                            input(f"Stop task {getattr(jobs[idx], 'id', '?')}? [y/N]: ")
                            .strip()
                            .lower()
                        )
                        if ans in ("y", "yes"):
                            store.cancel(getattr(jobs[idx], "id", ""))
                        return run_task_browser(console, mgr, session_id)
                elif ch == "\x1b":
                    break
                # Arrow keys: escape sequences
                elif ch == "\x1b":
                    break
    except Exception:
        jobs = _collect_jobs(mgr, session_id)
        for j in jobs:
            try:
                console.print(f"[{getattr(j, 'status', '?')}] {getattr(j, 'id', '?')}")
            except Exception:
                pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
        try:
            print(
                "[Task browser closed]  [Enter/O] output [S] stop [Tab] filter [R] refresh [Q] exit"
            )
        except Exception:
            pass
    # Simple arrow navigation via re-read of escape sequences is handled by
    # the interactive_menu.select_with_arrows fallback when raw mode is off.
    _ = time.time()
