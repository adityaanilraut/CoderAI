"""Tests for Phase 2: Input Subsystem & Bottom Toolbar Modernization."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from coderai.cli.app import _prompt_permissions
from coderai.cli.file_mention import suggest_workspace_files
from coderai.cli.statusline import compute_token_gauge, format_default_status_bar


# ==========================================
# 1. Tests for Single-Key & Enhanced Permissions
# ==========================================


def test_permission_prompt_single_key_yes(monkeypatch: pytest.MonkeyPatch):
    """Verify single-key 'y' approves permission request."""
    requests = [
        {
            "toolCallId": "call_123",
            "name": "bash",
            "command": "git pull",
            "scopes": ["query-git-log"],
            "risk_level": "LOW RISK",
        }
    ]

    monkeypatch.setattr("builtins.input", lambda _: "y")
    replies, always = _prompt_permissions(requests, yes=False, plan_mode=False)
    assert len(replies) == 1
    assert replies[0]["permission"] == "allow"
    assert replies[0]["toolCallId"] == "call_123"


def test_permission_prompt_single_key_no(monkeypatch: pytest.MonkeyPatch):
    """Verify single-key 'n' denies permission request."""
    requests = [
        {
            "toolCallId": "call_124",
            "name": "bash",
            "command": "rm file.txt",
            "scopes": ["delete-in-cwd"],
            "risk_level": "HIGH RISK",
        }
    ]

    monkeypatch.setattr("builtins.input", lambda _: "n")
    replies, always = _prompt_permissions(requests, yes=False, plan_mode=False)
    assert len(replies) == 1
    assert replies[0]["permission"] == "deny"


def test_permission_prompt_single_key_always(monkeypatch: pytest.MonkeyPatch):
    """Verify single-key 'a' sets always allow for scope."""
    requests = [
        {
            "toolCallId": "call_125",
            "name": "read",
            "scopes": ["read-in-cwd"],
            "risk_level": "LOW RISK",
        }
    ]

    monkeypatch.setattr("builtins.input", lambda _: "a")
    replies, always = _prompt_permissions(requests, yes=False, plan_mode=False)
    assert len(replies) == 1
    assert replies[0]["permission"] == "allow"
    assert "read-in-cwd" in always


def test_permission_prompt_in_place_edit_command(monkeypatch: pytest.MonkeyPatch):
    """Verify pressing 'e' allows in-place command modification before approval."""
    requests = [
        {
            "toolCallId": "call_126",
            "name": "bash",
            "command": "pytest test_foo.py",
            "scopes": ["write-in-cwd"],
            "input": {"command": "pytest test_foo.py"},
            "arguments": {"command": "pytest test_foo.py"},
            "risk_level": "MODERATE RISK",
        }
    ]

    # First input is 'e' (edit), second input is the new command
    inputs = iter(["e", "pytest test_foo.py -v --tb=short"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    replies, _ = _prompt_permissions(requests, yes=False, plan_mode=False)
    assert len(replies) == 1
    assert replies[0]["permission"] == "allow"
    assert replies[0]["command"] == "pytest test_foo.py -v --tb=short"
    assert requests[0]["command"] == "pytest test_foo.py -v --tb=short"
    assert requests[0]["input"]["command"] == "pytest test_foo.py -v --tb=short"


def test_permission_prompt_diff_toggle(monkeypatch: pytest.MonkeyPatch):
    """Verify pressing 'd' renders diff preview and re-prompts for final decision."""
    requests = [
        {
            "toolCallId": "call_127",
            "name": "write",
            "scopes": ["write-in-cwd"],
            "diff_preview": "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-old\n+new",
            "risk_level": "MODERATE RISK",
        }
    ]

    # First input 'd' (renders diff), then 'y' (approves)
    inputs = iter(["d", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    with patch("coderai.cli.app.render_diff_preview") as mock_diff:
        replies, _ = _prompt_permissions(requests, yes=False, plan_mode=False)
        assert mock_diff.called
        assert len(replies) == 1
        assert replies[0]["permission"] == "allow"


# ==========================================
# 2. Tests for Statusline & Token Gauges
# ==========================================


def test_compute_token_gauge_scales():
    """Verify token display computes percentages and compact window labels."""
    display, style, pct = compute_token_gauge(1000, "deepseek-v4-flash")
    assert "1,000" in display
    assert pct >= 0
    assert style in ("green", "yellow", "bold red")


def test_format_default_status_bar():
    """Verify status bar formats model, plan, git, and token information."""
    bar = format_default_status_bar(
        model="gpt-5.6-luna",
        active_tokens=5000,
        plan_mode=True,
        branch="main*",
        turns=3,
        mcp_count=2,
    )
    text = bar.plain
    assert "Model: gpt-5.6-luna" in text
    assert "Tokens:" in text
    assert "Plan: ON" in text
    assert "Git: main*" in text
    assert "Turns: 3" in text
    assert "MCP: 2" in text


# ==========================================
# 3. Tests for Workspace File Autocompletion
# ==========================================


def test_suggest_workspace_files_ranking(tmp_path):
    """Verify root files rank higher than deep nested files."""
    (tmp_path / "app.py").write_text("print(1)")
    (tmp_path / "deep" / "nested" / "sub").mkdir(parents=True)
    (tmp_path / "deep" / "nested" / "sub" / "app.py").write_text("print(2)")

    suggestions = suggest_workspace_files("app", str(tmp_path))
    assert len(suggestions) >= 2
    # Shallow app.py should appear before deep nested app.py
    assert suggestions[0] == "app.py"
