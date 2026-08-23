"""Tests for Phase 2 UI Enhancements: Arrow Selectors, Dedicated Tool Cards, Plan States."""

from __future__ import annotations

import json
from unittest.mock import patch

from rich.console import Console
from coderai.cli.interactive_menu import (
    prompt_plan_implementation,
    select_model_interactive,
    select_reasoning_effort_interactive,
    select_undo_interactive,
    select_with_arrows,
)
from coderai.cli.plan_render import format_plan_content, parse_plan_stats, render_plan_preview
from coderai.cli.tool_card import (
    _render_fetch_card,
    _render_lsp_card,
    _render_read_card,
    _render_search_grep_card,
    _render_subagent_card,
    render_tool_card,
)
from coderai.core.session import SessionMessage


def test_select_with_arrows_fallback_numeric():
    console = Console()
    items = [
        ("opt1", "Option One", "First option"),
        ("opt2", "Option Two", "Second option"),
        ("opt3", "Option Three", "Third option"),
    ]

    with patch("builtins.input", return_value="2"):
        res = select_with_arrows(console, items, title="Test Menu", default_idx=0)
        assert res == 1  # 0-indexed


def test_select_with_arrows_fallback_default_on_empty():
    console = Console()
    items = [
        ("opt1", "Option One", "First option"),
        ("opt2", "Option Two", "Second option"),
    ]

    with patch("builtins.input", return_value=""):
        res = select_with_arrows(console, items, title="Test Menu", default_idx=1)
        assert res == 1


def test_select_model_interactive_arrow_flow():
    console = Console()
    with patch("coderai.cli.interactive_menu.select_with_arrows", return_value=0):
        chosen = select_model_interactive(console, "gpt-5.6-luna")
        assert chosen == "gpt-5.6-sol"

    with patch("coderai.cli.interactive_menu.select_with_arrows", return_value="custom-model-id"):
        chosen_custom = select_model_interactive(console, "gpt-5.6-luna")
        assert chosen_custom == "custom-model-id"


def test_select_reasoning_effort_interactive():
    console = Console()
    with patch("coderai.cli.interactive_menu.select_with_arrows", return_value=1):
        effort = select_reasoning_effort_interactive(console, "max", model="gpt-5.6-sol")
        assert effort == "high"


def test_prompt_plan_implementation_choices():
    console = Console()
    with patch("coderai.cli.interactive_menu.select_with_arrows", return_value=0):
        act = prompt_plan_implementation(console, plan_text="- [ ] Task 1")
        assert act == "execute"

    with patch("coderai.cli.interactive_menu.select_with_arrows", return_value=1):
        act_refine = prompt_plan_implementation(console, plan_text="- [ ] Task 1")
        assert act_refine == "refine"


def test_select_undo_interactive_flow():
    console = Console()
    targets = [
        {"index": 1, "prompt": "Initial setup", "checkpoint_hash": "abc12345", "can_restore_code": True},
        {"index": 2, "prompt": "Add database schema", "checkpoint_hash": "def67890", "can_restore_code": True},
    ]

    with patch("coderai.cli.interactive_menu.select_with_arrows", side_effect=[0, 1]):
        target, mode = select_undo_interactive(console, targets)
        assert target is not None
        assert target["index"] == 1
        assert mode == "restore_conversation_only"


def test_plan_render_active_in_progress_state():
    plan_text = """
# Migration Plan
- [x] Step 1: Initialize database
- [>] Step 2: Run schema migration
- [ ] Step 3: Run regression tests
"""
    total, completed = parse_plan_stats(plan_text)
    assert total == 2  # [x] and [ ]
    assert completed == 1

    formatted = format_plan_content(plan_text)
    text_str = str(formatted)
    assert "Step 2: Run schema migration" in text_str

    console = Console()
    render_plan_preview(console, plan_text, title="Active Plan")


def test_dedicated_web_fetch_card():
    console = Console()
    metadata = {
        "url": "https://api.github.com/repos/deepseek",
        "status_code": 200,
        "bytes": 4500,
    }
    _render_fetch_card(console, "{\"name\": \"deepseek\"}", None, metadata, ok=True)


def test_dedicated_read_snippet_card():
    console = Console()
    metadata = {
        "file_path": "coderai/core/session.py",
        "snippet_id": "snip_a89f",
        "line_count": 25,
        "offset": 100,
    }
    _render_read_card(console, "coderai/core/session.py", metadata, "def test_func():\n    pass\n")


def test_dedicated_grep_search_card():
    console = Console()
    metadata = {
        "query": "SessionManager",
        "path": "coderai/",
        "matches_count": 12,
    }
    _render_search_grep_card(console, "coderai/core/session.py:100: class SessionManager\n", metadata, ok=True)


def test_dedicated_lsp_card():
    console = Console()
    metadata = {
        "file_path": "coderai/core/app.py",
        "diagnostics": [
            {"severity": "error", "line": 42, "col": 10, "message": "Type mismatch: expected int, got str", "code": "E012"},
            {"severity": "warning", "line": 50, "col": 1, "message": "Unused import: sys", "code": "W001"},
        ],
    }
    _render_lsp_card(console, None, metadata, ok=True)


def test_dedicated_subagent_card():
    console = Console()
    metadata = {
        "task_name": "Run pytest test suite",
        "agent_id": "agent_worker_9921",
    }
    _render_subagent_card(console, "73 tests passed in 0.44s", metadata, ok=True)


def test_render_tool_card_dispatches_all_dedicated_cards():
    console = Console()
    tool_cases = [
        ("WebFetch", {"url": "https://example.com", "status_code": 200}, "Fetched content"),
        ("read", {"file_path": "main.py", "snippet_id": "c1a2", "line_count": 10}, "x = 1\n"),
        ("grep", {"query": "import os", "path": "src/"}, "src/app.py:1: import os\n"),
        ("lsp", {"file_path": "app.py", "diagnostics": [{"severity": "error", "message": "fail"}]}, ""),
        ("subagent", {"task_name": "Explore repo", "agent_id": "sub_1"}, "Exploration complete."),
    ]

    for tool_name, meta, output in tool_cases:
        payload = {
            "ok": True,
            "name": tool_name,
            "output": output,
            "metadata": meta,
        }
        msg = SessionMessage(
            id=f"msg_{tool_name}",
            session_id="sess_1",
            role="tool",
            content=json.dumps(payload),
        )
        render_tool_card(console, msg)
