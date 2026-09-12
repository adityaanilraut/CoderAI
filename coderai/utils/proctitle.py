"""Process/terminal title helpers.

Lean stdlib-only port: ``setproctitle`` is optional; terminal TAB title uses
an ANSI OSC sequence and only fires on a TTY so piped output stays clean.
"""

from __future__ import annotations

import sys


def set_process_title(title: str) -> None:
    """Set the OS-level process title visible in ps/top (best-effort)."""
    try:
        import setproctitle  # type: ignore[import-not-found]

        setproctitle.setproctitle(title)
    except Exception:
        pass


def set_terminal_title(title: str) -> None:
    """Set the terminal tab/window title via OSC escape (TTY only)."""
    try:
        if not sys.stderr.isatty():
            return
        sys.stderr.write(f"\033]0;{title}\007")
        sys.stderr.flush()
    except OSError:
        pass


def init_process_name(name: str = "CoderAI") -> None:
    """Initialize process name: OS title + terminal tab title."""
    set_process_title(name)
    set_terminal_title(name)
