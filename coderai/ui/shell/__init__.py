"""Interactive shell package (Kimi structure)."""
from __future__ import annotations


# --- welcome section: from coderai/cli/welcome.py ---
"""Welcome screen and brand identity view for CoderAI CLI."""


import pathlib
import sys
from typing import Any

from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from coderai._version import __version__

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
    active_agent: str = "default",
) -> None:
    """Render the stylish CoderAI welcome screen with connection status and focused shortcuts."""
    from coderai.cli.ascii_art import get_gradient_ascii_logo  # lazy: avoids cli/__init__ cycle
    from coderai.core.common.model_capabilities import defaults_to_thinking_mode  # lazy
    from coderai.ui.shell.prompt import get_git_status  # lazy: prompt hub is heavy

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

        # Header Info Table inside Panel — clean 3-row structured grid
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right", ratio=1)

        # Row 1: Engine & Workspace
        r1_c1 = Text()
        r1_c1.append("  Engine: ", style="dim")
        r1_c1.append(f"v{__version__} ", style="bold")
        r1_c1.append(f"(Py {py_ver})", style="dim")

        r1_c2 = Text()
        r1_c2.append("Workspace: ", style="dim")
        r1_c2.append(f"{workspace_str}", style="bold")
        if branch:
            r1_c2.append(f" ({branch}{'*' if is_dirty else ''})", style="bold magenta")
        grid.add_row(r1_c1, r1_c2)

        # Row 2: Model & Status
        r2_c1 = Text()
        r2_c1.append("  Model: ", style="dim")
        r2_c1.append(f"{active_model}", style="bold cyan")

        r2_c2 = Text()
        r2_c2.append("Status: ", style="dim")
        if has_api_key:
            r2_c2.append("● Connected", style="bold green")
        else:
            r2_c2.append("○ No API Key (Run /setup)", style="bold yellow")
        if mcp_servers_count > 0:
            r2_c2.append(f" • MCP ({mcp_servers_count})", style="bold green")
        if skills_count > 0:
            r2_c2.append(f" • Skills ({skills_count})", style="bold yellow")
        grid.add_row(r2_c1, r2_c2)

        # Row 3: Agent, Reasoning & Plan Mode
        r3_c1 = Text()
        r3_c1.append("  Agent: ", style="dim")
        r3_c1.append(
            f"{active_agent}  ",
            style="bold magenta" if active_agent != "default" else "bold white",
        )
        r3_c1.append("•  Reasoning: ", style="dim")
        r3_c1.append(f"{thinking_str}", style="default")

        r3_c2 = Text()
        r3_c2.append("Plan Mode: ", style="dim")
        r3_c2.append(f"{plan_status}", style="bold yellow" if plan_mode else "dim")
        grid.add_row(r3_c1, r3_c2)

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
        actions.append("/agent", style="bold magenta")
        actions.append(" role  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/doctor", style="bold blue")
        actions.append(" diagnostics  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("/plan", style="bold yellow")
        actions.append(" safety  ", style="dim")
        actions.append("•  ", style="dim")
        actions.append("@file", style="bold cyan")
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
