"""Unit tests for Phase 4: Interactive shell, print visualize, live view, and theme."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kosong.message import Message, TextPart as KosongTextPart
from rich.console import Console
from rich.text import Text

from coderai.cli.ascii_art import CODERAI_ASCII_LOGO, get_gradient_ascii_logo
from coderai.ui.print.visualize import (
    FinalOnlyJsonPrinter,
    FinalOnlyTextPrinter,
    JsonPrinter,
    TextPrinter,
    visualize as print_visualize,
)
from coderai.ui.shell import render_welcome_screen
from coderai.ui.shell.echo import render_user_echo, render_user_echo_text
from coderai.ui.shell.mcp_status import render_mcp_console, render_mcp_prompt
from coderai.ui.shell.migration_nudge import (
    install_command,
    should_show_exit_nudge,
    verify_command,
)
from coderai.ui.shell.prompt import PROMPT_SYMBOL
from coderai.ui.shell.startup import ShellStartupProgress
from coderai.ui.shell.update import UpdateResult, semver_tuple
from coderai.ui.theme import (
    get_mcp_prompt_colors,
    get_prompt_style,
    get_toolbar_colors,
    set_active_theme,
)
from coderai.wire import Wire
from coderai.wire.types import (
    MCPServerSnapshot,
    MCPStatusSnapshot,
    StepBegin,
    TextPart as WireTextPart,
)


def test_ui_branding_and_welcome_logo_intact() -> None:
    """Ensure the ASCII logo and welcome screen remain unaltered."""
    assert len(CODERAI_ASCII_LOGO) == 6
    assert "██████╗" in CODERAI_ASCII_LOGO[0]

    logo = get_gradient_ascii_logo(force_full=True)
    assert isinstance(logo, Text)
    assert len(logo.plain) > 0

    console = Console(record=True)
    render_welcome_screen(
        console,
        project_root="/tmp/test-project",
        active_model="gemini-2.5-flash",
        plan_mode=True,
    )
    rendered = console.export_text()
    assert "CoderAI" in rendered
    assert "gemini-2.5-flash" in rendered
    assert "Plan Mode: ON" in rendered
    assert "/setup" in rendered


def test_user_echo_rendering() -> None:
    """Ensure user input echoing correctly displays prompt symbol."""
    msg = Message(role="user", content=[KosongTextPart(text="Hello CoderAI")])
    rendered = render_user_echo(msg)
    assert rendered.plain == f"{PROMPT_SYMBOL} Hello CoderAI"

    echo_text = render_user_echo_text("test prompt")
    assert echo_text.plain == f"{PROMPT_SYMBOL} test prompt"


def test_theme_prompt_colors() -> None:
    """Verify theme palette accessors."""
    set_active_theme("dark")
    dark_colors = get_mcp_prompt_colors()
    assert dark_colors.connected.startswith("fg:")

    dark_toolbar = get_toolbar_colors()
    assert "plan_label" in dark_toolbar.__dataclass_fields__

    style = get_prompt_style()
    assert style is not None

    set_active_theme("light")
    light_colors = get_mcp_prompt_colors()
    assert light_colors.connected.startswith("fg:")
    set_active_theme("dark")  # restore


def test_mcp_status_rendering() -> None:
    """Test console and prompt rendering for MCP status snapshots."""
    snapshot = MCPStatusSnapshot(
        total=2,
        connected=1,
        tools=3,
        loading=False,
        servers=[
            MCPServerSnapshot(name="github", status="connected", tools=["create_issue"]),
            MCPServerSnapshot(name="slack", status="connecting", tools=[]),
        ],
    )
    console_renderable = render_mcp_console(snapshot)
    assert console_renderable is not None

    snapshot_loading = MCPStatusSnapshot(
        total=1,
        connected=0,
        tools=0,
        loading=True,
        servers=[MCPServerSnapshot(name="fs", status="connecting", tools=[])],
    )
    prompt_formatted = render_mcp_prompt(snapshot_loading)
    assert len(prompt_formatted) > 0


def test_migration_nudge_helpers(tmp_path: Path) -> None:
    """Verify exit nudge date throttle and command strings."""
    assert "pip install" in install_command()
    assert "coderai" in verify_command()

    marker = tmp_path / ".migration-nudge"
    assert should_show_exit_nudge(marker, "2026-09-10") is True
    assert should_show_exit_nudge(marker, "2026-09-10") is False
    assert should_show_exit_nudge(marker, "2026-09-11") is True


def test_update_semver_parsing() -> None:
    """Verify version tuple parsing and comparison."""
    assert semver_tuple("0.4.0") == (0, 4, 0)
    assert semver_tuple("v1.2.3") == (1, 2, 3)
    assert semver_tuple("0.4.1") > semver_tuple("0.4.0")
    assert semver_tuple("1.0.0") > semver_tuple("0.99.99")
    assert UpdateResult.UP_TO_DATE != UpdateResult.UPDATED


def test_print_printers() -> None:
    """Test non-interactive Text, Json, and FinalOnly printers."""
    tp = TextPrinter()
    tp.feed(StepBegin(n=1))
    tp.flush()

    jp = JsonPrinter()
    jp.feed(StepBegin(n=1))
    jp.feed(WireTextPart(text="chunk1"))
    jp.flush()

    f_text = FinalOnlyTextPrinter()
    f_text.feed(WireTextPart(text="final output"))
    f_text.flush()

    f_json = FinalOnlyJsonPrinter()
    f_json.feed(WireTextPart(text="final json"))
    f_json.flush()


@pytest.mark.asyncio
async def test_print_visualize_stream() -> None:
    """Verify print_visualize consumes wire events until queue shutdown."""
    wire = Wire()
    task = asyncio.create_task(print_visualize("text", False, wire))
    wire.soul_side.send(StepBegin(n=1))
    wire.soul_side.send(WireTextPart(text="Hello wire"))
    wire.shutdown()
    await task


def test_startup_progress() -> None:
    """Test startup progress spinner helper."""
    progress = ShellStartupProgress(enabled=False)
    progress.update("Initializing MCP servers...")
    progress.stop()
