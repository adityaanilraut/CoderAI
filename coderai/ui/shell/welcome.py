"""Interactive shell package."""

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
from coderai.ui.shell.console import PANEL_BORDER_STYLE, PANEL_PADDING

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
    from coderai.utils.common.model_capabilities import defaults_to_thinking_mode  # lazy
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
        from coderai.llm import resolve_model_provider_routing
        from coderai.config import resolve_current_settings

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

        # Four fixed columns so labels share a vertical edge and long
        # values ellipsize instead of wrapping the row out of alignment.
        # Label width 12 fits the longest key ("Plan Mode:") at 80 columns.
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(width=12, no_wrap=True, style="dim")
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(width=12, no_wrap=True, style="dim")
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")

        workspace = Text(workspace_str, style="bold", no_wrap=True, overflow="ellipsis")
        if branch:
            workspace.append(f" ({branch}{'*' if is_dirty else ''})", style="bold magenta")

        status = Text(no_wrap=True, overflow="ellipsis")
        if has_api_key:
            status.append("● Connected", style="bold green")
        else:
            status.append("○ No API Key", style="bold yellow")

        tools = Text(no_wrap=True, overflow="ellipsis")
        tool_bits: list[str] = []
        if mcp_servers_count > 0:
            tool_bits.append(f"MCP {mcp_servers_count}")
        if skills_count > 0:
            tool_bits.append(f"Skills {skills_count}")
        tools.append(" · ".join(tool_bits) if tool_bits else "—", style="bold")

        grid.add_row(
            "Engine:",
            Text(f"v{__version__} (Py {py_ver})", style="bold", no_wrap=True, overflow="ellipsis"),
            "Workspace:",
            workspace,
        )
        grid.add_row(
            "Model:",
            Text(active_model, style="bold cyan", no_wrap=True, overflow="ellipsis"),
            "Status:",
            status,
        )
        grid.add_row(
            "Agent:",
            Text(
                active_agent,
                style="bold magenta" if active_agent != "default" else "bold",
                no_wrap=True,
                overflow="ellipsis",
            ),
            "Reasoning:",
            Text(thinking_str, no_wrap=True, overflow="ellipsis"),
        )
        grid.add_row(
            "Plan Mode:",
            Text(plan_status, style="bold yellow" if plan_mode else "dim"),
            "Tools:",
            tools,
        )

        panel = Panel(
            grid,
            title="[bold cyan]CoderAI[/] [dim]· Autonomous AI Pair Programming in your Terminal[/]",
            border_style=PANEL_BORDER_STYLE,
            padding=PANEL_PADDING,
        )
        console.print(panel)

        # Compact shortcuts — two short dim lines that fit 80-col terminals
        # without wrapping (one long rainbow line wrapped and looked broken).
        for row in (
            (
                ("/setup", "config"),
                ("/help", None),
                ("/agent", "role"),
                ("/doctor", "check"),
                ("/plan", "mode"),
            ),
            (("@file", "attach"), ("Tab", "complete"), ("Ctrl-C", "stop")),
        ):
            line = Text()
            line.append("  ", style="dim")
            for idx, (cmd, desc) in enumerate(row):
                if idx:
                    line.append(" · ", style="dim")
                line.append(cmd, style="bold")
                if desc:
                    line.append(f" {desc}", style="dim")
            console.print(line)
        console.print()
    else:
        print("\n" + str(get_gradient_ascii_logo()))
        branch_str = f" ({branch}{'*' if is_dirty else ''})" if branch else ""
        conn_str = "Connected" if has_api_key else "No API Key (Run /setup)"
        tool_bits = []
        if mcp_servers_count > 0:
            tool_bits.append(f"MCP {mcp_servers_count}")
        if skills_count > 0:
            tool_bits.append(f"Skills {skills_count}")
        tools_str = " · ".join(tool_bits) if tool_bits else "—"
        label_w = 12
        print(f"CoderAI v{__version__} — AI Pair Programming in your Terminal")
        print(f"  {'Engine:':<{label_w}} v{__version__} (Py {py_ver})")
        print(f"  {'Workspace:':<{label_w}} {workspace_str}{branch_str}")
        print(f"  {'Model:':<{label_w}} {active_model}")
        print(f"  {'Status:':<{label_w}} {conn_str}")
        print(f"  {'Agent:':<{label_w}} {active_agent}")
        print(f"  {'Reasoning:':<{label_w}} {thinking_str}")
        print(f"  {'Plan Mode:':<{label_w}} {plan_status}")
        print(f"  {'Tools:':<{label_w}} {tools_str}")
        print(
            "  /setup config · /help · /agent role · /doctor check · /plan mode\n"
            "  @file attach · Tab complete · Ctrl-C stop\n"
        )
