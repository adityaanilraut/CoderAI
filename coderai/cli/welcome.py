"""Welcome screen and brand identity view for CoderAI CLI."""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any

from coderai._version import __version__
from coderai.cli.ascii_art import get_gradient_ascii_logo
from coderai.core.common.model_capabilities import defaults_to_thinking_mode

try:
    from rich.align import Align
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Align = None  # type: ignore[assignment,misc]
    Console = None  # type: ignore[assignment,misc]
    Panel = None  # type: ignore[assignment,misc]
    Table = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


def get_git_branch(project_root: str) -> str | None:
    """Retrieve the current active git branch name if available."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0:
            branch = res.stdout.strip()
            if branch:
                return branch
    except Exception:
        pass
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
) -> None:
    """Render the stylish CoderAI welcome screen."""
    branch = get_git_branch(project_root)
    workspace_str = _format_workspace_path(project_root)
    branch_str = f" [bold cyan]({branch})[/]" if branch else ""
    thinking_str = "Enabled (Adaptive)" if defaults_to_thinking_mode(active_model) else "Adaptive"
    plan_status = "[bold yellow]ON[/]" if plan_mode else "[dim]OFF[/]"

    if (
        console is not None
        and _RICH
        and Panel is not None
        and Table is not None
        and Text is not None
    ):
        # ASCII Logo
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
        col1.append("  ✦ ", style="bold magenta")
        col1.append("Version: ", style="dim")
        col1.append(f"v{__version__}  ", style="bold white")
        col1.append("•  Model: ", style="dim")
        col1.append(f"{active_model}\n", style="bold cyan")
        col1.append("  ✦ ", style="bold magenta")
        col1.append("Thinking: ", style="dim")
        col1.append(f"{thinking_str}  ", style="white")
        col1.append("•  Plan Mode: ", style="dim")
        col1.append(f"{'ON' if plan_mode else 'OFF'}", style="bold yellow" if plan_mode else "dim")

        col2 = Text()
        col2.append("Workspace: ", style="dim")
        col2.append(f"{workspace_str}", style="bold white")
        if branch:
            col2.append(f" ({branch})", style="bold cyan")
        col2.append("\nTools: ", style="dim")
        col2.append("Snippet Read/Edit/Write • Bash • Web", style="white")
        if mcp_servers_count > 0:
            col2.append(f" • MCP ({mcp_servers_count})", style="green")

        grid.add_row(col1, col2)

        panel = Panel(
            grid,
            title="[bold cyan]CoderAI[/] [dim]• AI Pair Programming in your Terminal[/]",
            border_style="bright_blue",
            padding=(0, 1),
        )
        console.print(panel)

        # Quick Actions Bar
        actions = Text()
        actions.append("  Quick Actions:  ", style="bold white")
        actions.append("/help", style="bold cyan")
        actions.append(" commands  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/plan", style="bold yellow")
        actions.append(" toggle  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/model", style="bold magenta")
        actions.append(" switch  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/sessions", style="bold green")
        actions.append(" resume  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("@file", style="bold blue")
        actions.append(" mention", style="dim")
        console.print(actions)
        console.print()
    else:
        print("\n" + str(get_gradient_ascii_logo()))
        print(f"CoderAI v{__version__} — AI Pair Programming in your Terminal")
        print(
            f"Workspace: {workspace_str}{branch_str} | Model: {active_model} | Thinking: {thinking_str} | Plan: {plan_status}"
        )
        print(
            "Quick Actions: /help commands • /plan toggle • /model switch • /sessions resume • @file mention\n"
        )
