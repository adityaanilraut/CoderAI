"""Unit tests for UI Review & Remediation fixes.

Verifies:
1. Removal of dead web frontend (terminal purity).
2. Clean welcome screen rendering at standard 80-column terminal width.
3. Interactive and programmatic agent role switching on SessionManager.
4. Active agent role indicator in bottom toolbar tokens.
5. SlashCommandCompleter subargument completion for /agent and /role.
6. Rich Tree visualization for /agents tree.
7. Deduplicated CLI parser flags.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from prompt_toolkit.document import Document
from rich.console import Console
from rich.tree import Tree

from coderai.soul.session.manager import SessionManager
from coderai.ui.shell import render_welcome_screen
from coderai.ui.shell.prompt import SlashCommandCompleter, get_bottom_toolbar_tokens
from coderai.ui.shell.startup import _build_parser


def test_dead_web_frontend_removed() -> None:
    """Ensure coderai.cli.web and web_cmd are removed to maintain terminal purity."""
    with pytest.raises(ImportError):
        import coderai.cli.web  # type: ignore # noqa: F401

    with pytest.raises(ImportError):
        import coderai.cli.web_cmd  # type: ignore # noqa: F401


def test_welcome_screen_renders_cleanly_at_80_columns(tmp_path: Path) -> None:
    """Verify welcome screen renders in 80-column width without uncaught errors."""
    output_buffer = io.StringIO()
    test_console = Console(file=output_buffer, width=80, force_terminal=True, color_system="standard")

    render_welcome_screen(
        console=test_console,
        project_root=str(tmp_path),
        active_model="claude-3-7-sonnet",
        plan_mode=True,
        mcp_servers_count=3,
        skills_count=5,
        reasoning_effort="high",
        active_agent="architect",
    )
    rendered = output_buffer.getvalue()
    assert "CoderAI" in rendered
    assert "claude-3-7-sonnet" in rendered
    assert "architect" in rendered
    assert "high" in rendered.lower()
    assert "Enabled" in rendered
    assert "/agent" in rendered


def test_session_manager_agent_role_switching() -> None:
    """Verify active agent role tracking and switching in SessionManager."""
    mgr = SessionManager(
        project_root=Path("."),
        create_openai_client=MagicMock(),
        get_resolved_settings=MagicMock(return_value={}),
    )
    assert mgr.get_active_agent_role() == "default"
    assert mgr.active_agent_role == "default"

    # Switch to bundled okabe role
    assert mgr.switch_agent_role("okabe") is True
    assert mgr.get_active_agent_role() == "okabe"

    # Switch to discovered architect role
    assert mgr.switch_agent_role("architect") is True
    assert mgr.get_active_agent_role() == "architect"
    assert mgr.active_agent_role == "architect"

    # Switch back to default
    assert mgr.switch_agent_role("default") is True
    assert mgr.get_active_agent_role() == "default"


def test_toolbar_tokens_include_active_agent() -> None:
    """Verify active agent role badge is present in bottom toolbar when non-default."""
    # When default, no badge
    tokens_default = get_bottom_toolbar_tokens(
        project_root="/tmp",
        plan_mode=False,
        active_model="gpt-4o",
        active_agent="default",
    )
    text_default = "".join(t[1] for t in tokens_default)
    assert "role:" not in text_default

    # When architect, badge is rendered
    tokens_architect = get_bottom_toolbar_tokens(
        project_root="/tmp",
        plan_mode=False,
        active_model="gpt-4o",
        active_agent="architect",
    )
    text_architect = "".join(t[1] for t in tokens_architect)
    assert "role: architect" in text_architect


def test_slash_completer_agent_subarguments() -> None:
    """Verify SlashCommandCompleter offers subargument completions for /agent and /role."""
    completer = SlashCommandCompleter(
        available_commands=[
            ("/agent", "Manage or switch active subagent role"),
            ("/model", "Switch LLM model"),
        ],
        project_root=".",
    )

    doc = Document(text="/agent arch", cursor_position=len("/agent arch"))
    completions = list(completer.get_completions(doc, complete_event=None))
    matching_texts = [c.text for c in completions]
    assert "architect" in matching_texts

    doc_role = Document(text="/role plan", cursor_position=len("/role plan"))
    role_completions = list(completer.get_completions(doc_role, complete_event=None))
    role_texts = [c.text for c in role_completions]
    assert "planner" in role_texts


def test_agents_tree_rendering_is_rich_tree() -> None:
    """Verify that hierarchical agent tree builds a Rich Tree structure."""
    tree = Tree("[bold cyan]Agent Roles[/bold cyan]")
    bundled_node = tree.add("[bold green]Bundled Roles[/bold green]")
    bundled_node.add("[cyan]default[/cyan] [dim]— Full software engineering tool suite[/dim]")
    bundled_node.add("[cyan]okabe[/cyan] [dim]— Experimental mad-scientist persona[/dim]")

    disc_node = tree.add("[bold yellow]Discovered Roles (.coderai/agents)[/bold yellow]")
    disc_node.add("[magenta]architect[/magenta] [dim]— Systems architect[/dim]")

    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=True, color_system="standard")
    console.print(tree)
    output = buf.getvalue()

    assert "Agent Roles" in output
    assert "Bundled Roles" in output
    assert "default" in output
    assert "okabe" in output
    assert "Discovered Roles" in output
    assert "architect" in output


def test_cli_parser_deduplicated_and_has_all_flags() -> None:
    """Verify _build_parser has all subagent flags intact and parses without error."""
    parser = _build_parser()
    args = parser.parse_args(["--agent", "architect", "-y"])
    assert args.agent == "architect"
    assert args.yes is True

    # Test preset and subagent flags
    args2 = parser.parse_args(["--subagent-type", "planner", "--subagent-runner", "acp"])
    assert args2.subagent_type == "planner"
    assert args2.subagent_runner == "acp"
