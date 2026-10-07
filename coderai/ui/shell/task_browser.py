"""Interactive background-task browser.

Three-column TUI: task list | detail | output preview.
Keys: Enter/O full output in pager, S stop (confirm), Tab filter,
R refresh, Q/Esc exit. Auto-refreshes every second.
Falls back to a plain table when Rich/prompt_toolkit is unavailable.
"""

from __future__ import annotations

from typing import Any


def _collect_jobs(mgr: Any, session_id: str | None, active_only: bool = False) -> list[Any]:
    store = getattr(mgr, "job_store", None)
    if not store:
        return []
    jobs = store.snapshot(session_id) if session_id else []
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
    from coderai.utils.file_tail import tail_file

    path = getattr(job, "output_path", None)
    return (tail_file(path, lines=n) or "(no output yet)") if path else "(no output file)"


def task_browser_widths(term_cols: int) -> tuple[int, int, int]:
    """Pane widths that fit the terminal, including the gap between columns."""
    usable = max(1, term_cols)
    if usable < 80:
        return usable, 0, 0
    left = min(40, usable // 3)
    detail = min(28, usable // 4)
    return left, detail, usable - left - detail - 2


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
        nonlocal idx
        import shutil as _shutil

        from rich.console import Group
        from rich.text import Text

        try:
            term_cols = _shutil.get_terminal_size(fallback=(80, 24)).columns
        except Exception:
            term_cols = 80
        left_w, detail_w, preview_w = task_browser_widths(term_cols)
        jobs = _collect_jobs(mgr, session_id, active_only)
        if jobs and idx >= len(jobs):
            idx = len(jobs) - 1
        if not jobs:
            idx = 0
        left = Table(title="Tasks", border_style="cyan", width=left_w, expand=False)
        left.add_column("", width=2, no_wrap=True)
        left.add_column("#", width=3, justify="right", no_wrap=True)
        left.add_column("Status", width=8, no_wrap=True, overflow="ellipsis")
        left.add_column("Label", overflow="ellipsis", no_wrap=True)
        for i, j in enumerate(jobs):
            marker = "❯" if i == idx else " "
            status = str(getattr(j, "status", "?")).upper()
            left.add_row(marker, str(i + 1), status, str(getattr(j, "label", "")))
        if jobs and 0 <= idx < len(jobs):
            job = jobs[idx]
            detail = Table(title="Detail", border_style="cyan", width=detail_w, expand=False)
            detail.add_column("K", width=10, no_wrap=True)
            detail.add_column("V", overflow="fold")
            for k, v in _job_detail(job):
                detail.add_row(k, v)
            preview = Panel(
                _job_preview(job)[:1500],
                title="Output preview",
                border_style="cyan",
                width=preview_w,
            )
        else:
            detail = Panel("(no tasks)", title="Detail", border_style="cyan", width=detail_w)
            preview = Panel("", title="Output preview", border_style="cyan", width=preview_w)
        footer = Text.from_markup(
            "[dim][bold]↑/↓[/] move · [bold]Enter[/] output · [bold]S[/] stop · "
            "[bold]Tab[/] filter · [bold]R[/] refresh · [bold]Q[/] exit[/]"
        )
        body = (
            left
            if term_cols < 80
            else Columns([left, detail, preview], padding=(0, 1), expand=False)
        )
        return Group(body, Text(""), footer), jobs

    if sys.platform == "win32" or not sys.stdin.isatty():
        jobs = _collect_jobs(mgr, session_id)
        for j in jobs:
            console.print(
                f"[{getattr(j, 'status', '?')}] {getattr(j, 'id', '?')}"
            ) if console else print(j)
        return

    def _read_key(timeout: float) -> str | None:
        """Read one key. Arrow escapes move the selection; a bare Esc quits."""
        ready, _, _ = _select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            return ch
        follow, _, _ = _select.select([sys.stdin], [], [], 0.05)
        if not follow:
            return "esc"
        rest = ""
        while len(rest) < 3:
            more, _, _ = _select.select([sys.stdin], [], [], 0.01)
            if not more:
                break
            rest += sys.stdin.read(1)
        if rest.endswith("A"):
            return "up"
        if rest.endswith("B"):
            return "down"
        return "esc"

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            pending: tuple[str, Any] | None = None
            with Live(console=console, refresh_per_second=4, transient=True) as live:
                while True:
                    view, jobs = _render()
                    live.update(view)
                    key = _read_key(1.0)
                    if key is None:
                        continue
                    if key in ("q", "Q", "esc"):
                        return
                    if key == "\t":
                        active_only = not active_only
                        idx = 0
                        continue
                    if key in ("r", "R"):
                        continue
                    if key == "up" and jobs:
                        idx = (idx - 1) % len(jobs)
                        continue
                    if key == "down" and jobs:
                        idx = (idx + 1) % len(jobs)
                        continue
                    if key in ("\r", "\n", "o", "O", "s", "S"):
                        chosen = jobs[idx] if jobs and 0 <= idx < len(jobs) else None
                        pending = (key, chosen)
                        break
            if pending is None:
                return
            key, chosen = pending
            if key in ("\r", "\n", "o", "O") and chosen is not None:
                print(_job_preview(chosen, n=200))
                input("Press Enter to return...")
                continue
            if key in ("s", "S") and chosen is not None and store:
                ans = input(f"Stop task {getattr(chosen, 'id', '?')}? [y/N]: ").strip().lower()
                if ans in ("y", "yes"):
                    store.kill(getattr(chosen, "id", ""), session_id)
                continue
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
