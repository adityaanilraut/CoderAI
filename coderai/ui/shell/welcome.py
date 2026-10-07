"""Responsive terminal welcome; shared values for rich and accessible output."""

from __future__ import annotations

import pathlib
from typing import Any
from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from coderai._version import __version__


def _format_workspace_path(project_root: str) -> str:
    path = pathlib.Path(project_root)
    try:
        return "~/" + str(path.relative_to(pathlib.Path.home()))
    except ValueError:
        return str(path)


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
    from coderai.ui.shell.prompt import get_git_status
    from coderai.ui.shell.preferences import accessible_enabled
    from coderai.utils.common.model_capabilities import defaults_to_thinking_mode
    from coderai.config import resolve_current_settings
    from coderai.ui.shell.model_details import connection_label

    branch, dirty = get_git_status(project_root)
    status = "Connection status unavailable; /model verify"
    try:
        status = connection_label(
            project_root, active_model, resolve_current_settings(project_root)
        )
    except (OSError, ValueError):
        pass
    effort = (reasoning_effort or "max").capitalize()
    rows = [
        (
            "Workspace",
            _format_workspace_path(project_root)
            + (f" ({branch}{'*' if dirty else ''})" if branch else ""),
        ),
        ("Model", active_model),
        ("Status", status),
        ("Agent", active_agent),
        ("Reasoning", f"Enabled ({effort})" if defaults_to_thinking_mode(active_model) else effort),
        ("Plan Mode", "ON" if plan_mode else "OFF"),
        ("Tools", f"MCP {mcp_servers_count} | Skills {skills_count}"),
    ]
    shortcuts = "/help | /setup | /agent | /activity | /plan"
    controls = "Enter send/queue | Ctrl-S steer | Ctrl-C stop"
    if console is None or accessible_enabled():
        print(f"\nCoderAI v{__version__}")
        for label, value in rows:
            print(f"{label}: {value}")
        print(shortcuts + "\n" + controls + "\n")
        return
    from coderai.cli.ascii_art import get_compact_gradient_badge, get_gradient_ascii_logo

    # Use this console's width, which may differ from the process terminal.
    logo = (
        get_gradient_ascii_logo(force_full=True)
        if console.width >= 58
        else get_compact_gradient_badge()
    )
    console.print()
    console.print(Align.center(logo))
    # Values wrap rather than hiding model identity or verification status.
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    for label, value in rows:
        grid.add_row(label + ":", Text(value))
    console.print(Panel(grid, title=f"CoderAI v{__version__}", border_style="cyan"))
    console.print(Text(shortcuts, style="dim"))
    console.print(Text(controls, style="dim"))
    console.print()
