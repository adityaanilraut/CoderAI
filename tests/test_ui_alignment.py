"""Alignment and chrome consistency for the terminal UI."""

from __future__ import annotations

import io
import re
from pathlib import Path

from rich.console import Console

from coderai.ui.shell.console import kv_table
from coderai.ui.shell.prompt import get_bottom_toolbar_tokens, make_mini_bar
from coderai.ui.shell.slash import _help_command_column_width, _render_plain_overview
from coderai.ui.shell.task_browser import task_browser_widths
from coderai.ui.shell.visualize._approval_panel import ApprovalRequestPanel
from coderai.ui.shell.visualize._btw_panel import BtwPanel
from coderai.ui.shell.visualize._question_panel import QuestionRequestPanel, questions_to_request
from coderai.ui.shell.welcome import render_welcome_screen
from coderai.ui.theme import get_prompt_session_styles, set_active_theme
from coderai.wire.types import ApprovalRequest

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(renderable: object, width: int = 80) -> str:
    buf = io.StringIO()
    Console(file=buf, width=width, force_terminal=True, color_system="truecolor").print(renderable)
    return _ANSI.sub("", buf.getvalue())


def _bracket_columns(text: str) -> list[int]:
    cols = []
    for line in text.splitlines():
        if re.search(r"\[\d+\]", line):
            cols.append(line.index("["))
    return cols


def test_welcome_pairs_share_a_row(tmp_path: Path) -> None:
    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=True, color_system="standard")
    render_welcome_screen(
        console=console,
        project_root=str(tmp_path),
        active_model="claude-3-7-sonnet",
        plan_mode=True,
        active_agent="architect",
        reasoning_effort="high",
    )
    rendered = _ANSI.sub("", buf.getvalue())
    assert any("Agent:" in line and "Reasoning:" in line for line in rendered.splitlines())
    assert any("Plan Mode:" in line and "ON" in line for line in rendered.splitlines())
    assert any("Model:" in line and "Status:" in line for line in rendered.splitlines())


def test_toolbar_uses_a_single_separator() -> None:
    tokens = get_bottom_toolbar_tokens(
        project_root="/tmp/workspace",
        plan_mode=True,
        active_model="gpt-4o",
        active_agent="architect",
        yolo=True,
        afk=True,
        turns=2,
        mcp_count=1,
        tokens=1200,
    )
    text = "".join(part for _, part in tokens)
    assert "role: architect" in text
    assert " · " in text
    assert "  ·  " not in text
    assert any(style == "class:toolbar.role" for style, _ in tokens)


def test_toolbar_styles_share_a_background() -> None:
    set_active_theme("dark")
    dark = get_prompt_session_styles()
    for key, value in dark.items():
        if key.startswith("toolbar"):
            assert "bg:#1e1e2e" in value, key
    set_active_theme("light")
    try:
        light = get_prompt_session_styles()
        for key, value in light.items():
            if key.startswith("toolbar"):
                assert "bg:#f1f5f9" in value, key
        assert light["toolbar.model"] != dark["toolbar.model"]
    finally:
        set_active_theme("dark")


def test_question_and_approval_options_share_a_column() -> None:
    questions = questions_to_request(
        [
            {
                "question": "Which approach?",
                "options": [
                    {"label": "Fast", "description": "ship now"},
                    {"label": "Careful", "description": "more tests"},
                ],
            }
        ]
    )
    question = _plain(QuestionRequestPanel(questions).render())
    brackets = _bracket_columns(question)
    assert len(brackets) >= 2
    assert len(set(brackets)) == 1

    request = ApprovalRequest(
        id="1",
        tool_call_id="1",
        sender="write",
        action="write test.py",
        description="update the file",
    )
    approval = _plain(ApprovalRequestPanel(request).render())
    approval_cols = _bracket_columns(approval)
    assert len(approval_cols) >= 2
    assert len(set(approval_cols)) == 1


def test_btw_rule_matches_panel_inner_width() -> None:
    panel = BtwPanel()
    panel.set_question("Why?")
    panel.set_result("Because.", None)
    text = _plain(panel.render(80), width=80)
    rules = [line for line in text.splitlines() if line.startswith("│") and line.count("─") == 76]
    assert len(rules) == 1


def test_context_bar_clamps_when_over_budget() -> None:
    assert len(make_mini_bar(150, width=10)) == 10
    assert len(make_mini_bar(-4, width=10)) == 10
    assert set(make_mini_bar(150, width=10)) == {"■"}


def test_kv_labels_are_not_clipped() -> None:
    text = _plain(
        kv_table(
            [
                ("Configured MCP Servers:", "none"),
                ("Active Working Context:", "1"),
            ]
        )
    )
    assert "Configured MCP Servers:" in text
    assert "Active Working Context:" in text


def test_help_descriptions_share_a_column(capsys) -> None:  # type: ignore[no-untyped-def]
    width = _help_command_column_width(80, cap=False)
    _render_plain_overview()
    out = capsys.readouterr().out
    desc_at = 2 + width + 2
    command_lines = [
        line
        for line in out.splitlines()
        if line.startswith("  /") and len(line) > desc_at and line[desc_at - 2 : desc_at] == "  "
    ]
    assert len(command_lines) >= 3
    assert all(line[desc_at - 1] != " " or line[desc_at] != " " for line in command_lines[:5])


def test_task_browser_panes_fit_the_terminal() -> None:
    for cols in (40, 60, 80, 120, 200):
        left, detail, preview = task_browser_widths(cols)
        usable = max(48, cols)
        assert left + detail + preview + 2 <= usable
        assert min(left, detail, preview) >= 12
