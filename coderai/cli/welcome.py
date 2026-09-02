"""Welcome screen and brand identity view for CoderAI CLI."""

from __future__ import annotations

import pathlib
import sys
from typing import Any

from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from coderai._version import __version__
from coderai.cli.ascii_art import get_gradient_ascii_logo
from coderai.cli.statusline import get_git_status
from coderai.core.common.model_capabilities import defaults_to_thinking_mode

_RICH = True


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
    """Render the stylish CoderAI welcome screen with connection status and focused shortcuts."""
    branch, is_dirty = get_git_status(project_root)
    workspace_str = _format_workspace_path(project_root)
    effort_norm = (reasoning_effort or "max").capitalize()
    thinking_str = (
        f"Enabled ({effort_norm})" if defaults_to_thinking_mode(active_model) else effort_norm
    )
    plan_status = "ON" if plan_mode else "OFF"
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"

    # Check API key configuration status
    has_api_key = False
    try:
        from coderai.core.openai_client import resolve_model_provider_routing
        from coderai.core.settings import resolve_current_settings

        cur_settings = resolve_current_settings(project_root)
        _, resolved_key = resolve_model_provider_routing(
            active_model,
            explicit_base_url=cur_settings.get("baseURL"),
            explicit_api_key=cur_settings.get("apiKey"),
        )
        has_api_key = bool(resolved_key)
    except Exception:
        pass

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
        col1.append(f"(Py {py_ver})  ", style="dim")
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
        col2.append("\nStatus: ", style="dim")
        if has_api_key:
            col2.append("● Connected", style="bold green")
        else:
            col2.append("○ No API Key (Run /setup)", style="bold yellow")

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

        # Streamlined Quick Actions Bar
        actions = Text()
        actions.append("  Shortcuts:  ", style="bold")
        actions.append("/setup", style="bold green")
        actions.append(" configure  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/help", style="bold cyan")
        actions.append(" manual  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/doctor", style="bold magenta")
        actions.append(" diagnostics  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/plan", style="bold yellow")
        actions.append(" safety  ", style="dim")
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
        conn_str = "Connected" if has_api_key else "No API Key (Run /setup)"
        print(f"CoderAI v{__version__} — AI Pair Programming in your Terminal")
        print(
            f"Workspace: {workspace_str}{branch_str} | Model: {active_model} | Status: {conn_str} | Plan: {plan_status}"
        )
        print(
            "Shortcuts: /setup configure • /help manual • /doctor diagnostics • /plan safety • @file context • Ctrl-R search • Ctrl-C interrupt • Tab complete\n"
        )
