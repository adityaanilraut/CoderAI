"""Welcome screen and brand identity view for CoderAI CLI."""

from __future__ import annotations

import pathlib
import platform
import subprocess
import sys
from typing import Any

from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from coderai._version import __version__
from coderai.cli.ascii_art import get_gradient_ascii_logo
from coderai.core.common.model_capabilities import defaults_to_thinking_mode

_RICH = True


def get_git_status(project_root: str) -> tuple[str | None, bool]:
    """Retrieve the current active git branch and dirty status."""
    branch: str | None = None
    is_dirty = False
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0:
            b = res.stdout.strip()
            if b:
                branch = b
        if branch:
            res_dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res_dirty.returncode == 0 and res_dirty.stdout.strip():
                is_dirty = True
    except Exception:
        pass
    return branch, is_dirty


def get_git_branch(project_root: str) -> str | None:
    """Retrieve the current active git branch name if available (backward compatible)."""
    branch, is_dirty = get_git_status(project_root)
    if branch:
        return f"{branch}*" if is_dirty else branch
    return None


def _format_workspace_path(project_root: str) -> str:
    home = str(pathlib.Path.home())
    if project_root.startswith(home):
        return "~" + project_root[len(home) :]
    return project_root


def render_welcome_screen(
    console: Any | None,
    project_root: str,
    active_model: str,
    plan_mode: bool = False,
    mcp_servers_count: int = 0,
    skills_count: int = 0,
    reasoning_effort: str = "max",
) -> None:
    """Render the stylish CoderAI welcome screen."""
    branch, is_dirty = get_git_status(project_root)
    workspace_str = _format_workspace_path(project_root)
    effort_norm = (reasoning_effort or "max").capitalize()
    thinking_str = (
        f"Enabled ({effort_norm})" if defaults_to_thinking_mode(active_model) else effort_norm
    )
    plan_status = "ON" if plan_mode else "OFF"
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    sys_tag = platform.system()

    if (
        console is not None
        and _RICH
        and Panel is not None
        and Table is not None
        and Text is not None
    ):
        # ASCII Logo or Compact Badge
        logo = get_gradient_ascii_logo()
        console.print()
        if isinstance(logo, Text):
            console.print(Align.center(logo))
        else:
            console.print(logo)

        # Header Info Table inside Panel
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right", ratio=1)

        col1 = Text()
        col1.append("  Engine: ", style="dim")
        col1.append(f"v{__version__} ", style="bold")
        col1.append(f"(Py {py_ver} • {sys_tag})  ", style="dim")
        col1.append("•  Model: ", style="dim")
        col1.append(f"{active_model}\n", style="bold cyan")

        col1.append("  Reasoning: ", style="dim")
        col1.append(f"{thinking_str}  ", style="default")
        col1.append("•  Plan Mode: ", style="dim")
        col1.append(f"{plan_status}", style="bold yellow" if plan_mode else "dim")

        col2 = Text()
        col2.append("Workspace: ", style="dim")
        col2.append(f"{workspace_str}", style="bold")
        if branch:
            col2.append(f" ({branch}{'*' if is_dirty else ''})", style="bold magenta")
        col2.append("\nCapabilities: ", style="dim")
        col2.append("Scoped Snippets • Bash • Web", style="default")
        if mcp_servers_count > 0:
            col2.append(f" • MCP ({mcp_servers_count})", style="bold green")
        if skills_count > 0:
            col2.append(f" • Skills ({skills_count})", style="bold yellow")

        grid.add_row(col1, col2)

        panel = Panel(
            grid,
            title="[bold cyan]CoderAI[/] [dim]• Autonomous AI Pair Programming in your Terminal[/]",
            border_style="bright_blue",
            padding=(0, 1),
        )
        console.print(panel)

        # Quick Actions Bar & Cheat Sheet
        actions = Text()
        actions.append("  Shortcuts:  ", style="bold")
        actions.append("/setup", style="bold green")
        actions.append(" keys & models  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/help", style="bold cyan")
        actions.append(" manual  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/doctor", style="bold magenta")
        actions.append(" diagnostics  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/plan", style="bold yellow")
        actions.append(" plan mode  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/model", style="bold cyan")
        actions.append(" switch  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("@file", style="bold blue")
        actions.append(" context  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("Ctrl-R", style="bold")
        actions.append(" search  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("Ctrl-C", style="bold red")
        actions.append(" interrupt  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("Tab", style="bold")
        actions.append(" complete", style="dim")
        console.print(actions)
        console.print()
    else:
        print("\n" + str(get_gradient_ascii_logo()))
        branch_str = f" ({branch}{'*' if is_dirty else ''})" if branch else ""
        print(f"CoderAI v{__version__} — AI Pair Programming in your Terminal")
        print(
            f"Workspace: {workspace_str}{branch_str} | Model: {active_model} | Reasoning: {thinking_str} | Plan: {plan_status}"
        )
        print(
            "Shortcuts: /setup keys & models • /help commands • /doctor health • /plan toggle • /model switch • Ctrl-R history • Ctrl-C interrupt • Tab complete • @file context\n"
        )

