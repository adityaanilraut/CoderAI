from __future__ import annotations

import contextlib
from pathlib import Path

from rich.text import Text

_INSTALL_CMD = "pip install -U coderai-agent"


def install_command(platform: str = "darwin") -> str:
    """Return the upgrade / install command to DISPLAY."""
    return _INSTALL_CMD


def install_run_command(platform: str = "darwin") -> str:
    """Return the install command in a form runnable via the shell."""
    return _INSTALL_CMD


def verify_command(platform: str = "darwin") -> str:
    """Command to check which `coderai` resolves on PATH (Windows: where, else: which)."""
    return "where coderai" if platform == "win32" else "which coderai"


def coderai_installed(home: Path | None = None) -> bool:
    """True if CoderAI workspace/data directory ~/.coderai exists."""
    home = home or Path.home()
    return (home / ".coderai").is_dir()


def exit_nudge_marker(share_dir: Path) -> Path:
    """Path of the throttle marker recording the last day the exit nudge was shown."""
    return share_dir / ".migration-nudge"


def should_show_exit_nudge(marker: Path, today: str) -> bool:
    """Return True at most once per calendar day; record `today` when returning True.

    `today` is an ISO date string (e.g. "2026-06-05"), injected for testability.
    """
    try:
        last = marker.read_text(encoding="utf-8").strip()
    except OSError:
        last = ""
    if last == today:
        return False
    with contextlib.suppress(OSError):
        marker.write_text(today, encoding="utf-8")
    return True


def welcome_card_text() -> Text:
    """Welcome-screen card nudging users to stay updated."""
    return Text.assemble(
        "CoderAI — Autonomous AI pair programming in your terminal.\n",
        "Run ",
        ("/upgrade", "bold"),
        " to check for the latest improvements; your config & sessions carry over.",
    )


def already_installed_text(platform: str = "darwin") -> Text:
    """Welcome-screen note showing CoderAI installation status."""
    return Text.assemble(
        "CoderAI is installed and ready. Start it in any project with ",
        ("coderai", "bold"),
        " (verify: ",
        (verify_command(platform), "cyan"),
        " → ~/.coderai).",
    )


def exit_nudge_text(platform: str = "darwin") -> Text:
    """Throttled tip printed on graceful exit."""
    return Text.assemble(
        ("Tip: ", "yellow"),
        "Keep CoderAI updated for the latest model capabilities and bugfixes.\n",
        "Update: ",
        (install_command(platform), "cyan"),
        ("  (or run /upgrade in session)", "grey50"),
    )
