"""Dynamic status line and prompt bar for interactive REPL."""

from __future__ import annotations

import subprocess
from typing import Any

try:
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


def get_git_branch_cached(project_root: str) -> str:
    """Get the active git branch name or 'detached'/'none'."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=1,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "no-git"


def format_status_bar(
    model: str,
    active_tokens: int,
    plan_mode: bool,
    branch: str,
) -> Text | str:
    """Format a dynamic status bar: [Model: <name>] [Tokens: <active>] [Plan: ON/OFF] [Git: <branch>]."""
    plan_label = "ON" if plan_mode else "OFF"
    tokens_formatted = f"{active_tokens:,}"

    if not _RICH or Text is None:
        return f"[Model: {model}] [Tokens: {tokens_formatted}] [Plan: {plan_label}] [Git: {branch}]"

    bar = Text()
    bar.append(" [", style="dim")
    bar.append("Model: ", style="dim cyan")
    bar.append(model, style="bold cyan")
    bar.append("] [", style="dim")
    bar.append("Tokens: ", style="dim green")
    bar.append(tokens_formatted, style="bold green")
    bar.append("] [", style="dim")
    bar.append("Plan: ", style="dim yellow")
    bar.append(plan_label, style="bold yellow" if plan_mode else "dim white")
    bar.append("] [", style="dim")
    bar.append("Git: ", style="dim magenta")
    bar.append(branch, style="bold magenta")
    bar.append("]", style="dim")
    return bar


def render_status_bar(
    console: Any | None,
    model: str,
    active_tokens: int,
    plan_mode: bool,
    project_root: str,
) -> None:
    """Render the status bar line above the REPL input prompt."""
    branch = get_git_branch_cached(project_root)
    bar = format_status_bar(model, active_tokens, plan_mode, branch)
    if console is not None and _RICH:
        console.print()
        console.print(bar)
    else:
        print(f"\n{bar}")
