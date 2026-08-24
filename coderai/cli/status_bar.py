"""Dynamic status line and prompt bar for interactive REPL."""

from __future__ import annotations

from typing import Any

from rich.text import Text

from coderai.cli.statusline import StatuslineEngine, compute_token_gauge

__all__ = ["compute_token_gauge", "format_status_bar", "render_status_bar"]

_ENGINE = StatuslineEngine()


def format_status_bar(
    model: str,
    active_tokens: int,
    plan_mode: bool,
    branch: str,
    turns: int = 0,
    mcp_count: int = 0,
) -> Text:
    """Format a dynamic status bar: [Model: <name>] [Tokens: <active> (<% of max>)] [Plan: ON/OFF] [Git: <branch>]."""
    return _ENGINE.format_default_status_bar(
        model, active_tokens, plan_mode, branch, turns=turns, mcp_count=mcp_count
    )


def render_status_bar(
    console: Any | None,
    model: str,
    active_tokens: int,
    plan_mode: bool,
    project_root: str,
    turns: int = 0,
    mcp_count: int = 0,
    settings: dict[str, Any] | None = None,
) -> None:
    """Render the status bar line above the REPL input prompt."""
    engine = StatuslineEngine(settings) if settings else _ENGINE
    engine.render(
        console,
        model,
        active_tokens,
        plan_mode,
        project_root,
        turns=turns,
        mcp_count=mcp_count,
    )
