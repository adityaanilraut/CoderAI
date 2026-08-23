"""Unit tests verifying the 13 CLI review findings, fixes, parity, and enhancements."""

import json
import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from coderai.cli.app import (
    COMMAND_HELP_ALIASES,
    COMMAND_HELP_DETAILS,
    _render_help_menu,
)
from coderai.cli.completer import (
    AVAILABLE_SLASH_COMMANDS,
    CoderAICompleter,
)
from coderai.cli.doctor import (
    DiagnosticItem,
    DoctorReport,
    mask_secret,
    render_doctor,
    run_doctor_diagnostics,
)
from coderai.cli.image_attachment import (
    detect_image_dimensions,
    parse_and_attach_image,
    resolve_image_path,
)
from coderai.cli.input_engine import (
    count_code_fences,
    count_triple_quotes,
    is_multiline_incomplete,
    normalize_multiline_input,
    open_external_editor,
    read_paste_mode,
)
from coderai.cli.interactive_menu import select_session_interactive
from coderai.cli.welcome import render_welcome_screen
from coderai.core.session import SessionEntry, SessionManager, SessionMessage


def test_help_menu_plain_text_and_rich_parity(capsys):
    """Point 1 & 3: Ensure plain-text help menu and rich table include all commands and aliases."""
    # Test rich mode
    _render_help_menu()
    # Test plain text fallback
    with patch("coderai.cli.app._RICH", False):
        _render_help_menu()
        captured = capsys.readouterr().out
        assert "/permission" in captured
        assert "/goal" in captured
        assert "/doctor" in captured
        assert "/jobs" in captured
        assert "/schedule" in captured
        assert "/agents" in captured
        assert "/image" in captured
        assert "/editor" in captured
        assert "/paste" in captured
        assert "/rename" in captured
        assert "/tokens, /cost" in captured
        assert "/config, /settings" in captured
        assert "/delete, /rm" in captured
        assert "Shortcuts:" in captured


def test_contextual_help_commands(capsys):
    """Point 4 & 5: Ensure contextual help /help <cmd> works with detailed syntax and examples."""
    for cmd_key in ["plan", "goal", "mcp", "permission", "doctor", "jobs", "schedule", "agents", "image", "rename", "shortcuts"]:
        # Test rich
        _render_help_menu(cmd_key)
        # Test plain text
        with patch("coderai.cli.app._RICH", False):
            _render_help_menu(cmd_key)
            captured = capsys.readouterr().out
            assert f"/{cmd_key}" in captured or "Help" in captured
            assert "Syntax:" in captured
            assert "Examples:" in captured

    # Test alias lookup e.g. /help cost -> tokens
    with patch("coderai.cli.app._RICH", False):
        _render_help_menu("cost")
        captured = capsys.readouterr().out
        assert "token usage breakdown and cost estimation" in captured


def test_completer_all_commands_and_aliases():
    """Point 2: Ensure completer has all slash commands and working aliases."""
    cmd_dict = dict(AVAILABLE_SLASH_COMMANDS)
    expected_cmds = [
        "/help", "/?", "/doctor", "/plan", "/undo", "/diff", "/model", "/sessions",
        "/resume", "/fork", "/delete", "/rm", "/rename", "/new", "/init", "/skills",
        "/skill", "/jobs", "/job", "/schedule", "/agents", "/subagents", "/teams",
        "/lsp", "/mcp", "/compact", "/tokens", "/cost", "/config", "/settings",
        "/permission", "/permissions", "/goal", "/image", "/editor", "/edit",
        "/paste", "/history", "/export", "/thinking", "/raw", "/clear", "/continue",
        "/exit", "/quit",
    ]
    for c in expected_cmds:
        assert c in cmd_dict, f"Command {c} missing from AVAILABLE_SLASH_COMMANDS"


def test_completer_sub_arguments(tmp_path: pathlib.Path):
    """Point 12: Ensure sub-argument autocompletion works for permissions, goals, plans, etc."""
    completer = CoderAICompleter(str(tmp_path))

    # Helper to mock readline buffer
    def mock_line(buf: str, text: str = ""):
        with patch("readline.get_line_buffer", return_value=buf):
            return completer.complete(text or buf, 0)

    # /permission -> read-only
    res = mock_line("/permission read", "read")
    assert res == "read-only"

    # /goal -> list/add/done
    res = mock_line("/goal ad", "ad")
    assert res == "add"

    # /plan -> apply
    res = mock_line("/plan ap", "ap")
    assert res == "apply"

    # /thinking -> full
    res = mock_line("/thinking fu", "fu")
    assert res == "full"

    # /mcp -> reconnect
    res = mock_line("/mcp rec", "rec")
    assert res == "reconnect"

    # /jobs -> kill
    res = mock_line("/jobs ki", "ki")
    assert res == "kill"

    # /schedule -> after
    res = mock_line("/schedule af", "af")
    assert res == "after"

    # /agents -> tree
    res = mock_line("/agents tr", "tr")
    assert res == "tree"

    # /help -> doctor
    res = mock_line("/help doc", "doc")
    assert res == "doctor"


def test_welcome_screen_shortcuts(tmp_path: pathlib.Path, capsys):
    """Point 6: Ensure welcome screen documents keyboard shortcuts and doctor."""
    render_welcome_screen(None, str(tmp_path), "gpt-5.6-luna", plan_mode=False)
    captured = capsys.readouterr().out
    assert "Ctrl-R" in captured
    assert "Ctrl-C" in captured
    assert "/doctor" in captured
    assert "Tab" in captured


def test_multiline_and_editor_input(tmp_path: pathlib.Path):
    """Point 7: Ensure multiline parsing, triple quotes, and normalize input."""
    # Triple quotes
    assert count_triple_quotes('"""test"""') == 2
    assert is_multiline_incomplete(['"""line 1']) is True
    assert is_multiline_incomplete(['"""line 1', 'line 2"""']) is False

    # Backslash
    assert is_multiline_incomplete(["step 1 \\"]) is True
    normalized = normalize_multiline_input("line 1 \\\nline 2")
    assert normalized == "line 1 line 2"

    # Triple quote stripping when used as envelope
    stripped = normalize_multiline_input('"""my long prompt"""')
    assert stripped == "my long prompt"

    # Paste mode
    inputs = iter(["line 1", "line 2", ":::"])
    pasted = read_paste_mode(input_func=lambda _: next(inputs))
    assert pasted == "line 1\nline 2"

    # External editor mock
    with patch("subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("builtins.open", MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=MagicMock(read=lambda: "edited prompt content"))))):
        content = open_external_editor("initial")
        assert content == "edited prompt content"


def test_doctor_diagnostics(tmp_path: pathlib.Path, capsys):
    """Point 8: Ensure /doctor diagnostics runs probes and renders report."""
    mgr = MagicMock()
    mgr.project_root = str(tmp_path)
    mgr.get_active_model.return_value = "deepseek-v4-pro"
    mgr.get_resolved_settings.return_value = {}

    report = run_doctor_diagnostics(str(tmp_path), mgr)
    assert isinstance(report, DoctorReport)
    assert len(report.items) >= 5

    categories = [i.category for i in report.items]
    assert "Runtime" in categories
    assert "Workspace" in categories
    assert "LLM / Provider" in categories
    assert "MCP Extensibility" in categories
    assert "Persistence" in categories

    # Render report in plain text
    with patch("coderai.cli.doctor._RICH", False):
        render_doctor(None, report)
        captured = capsys.readouterr().out
        assert "CoderAI System Doctor Diagnostics" in captured
        assert "Runtime" in captured


def test_image_attachment(tmp_path: pathlib.Path):
    """Point 10: Ensure image attachment parsing and base64 encoding."""
    # Create sample PNG header
    # PNG signature: 89 50 4E 47 0D 0A 1A 0A
    # IHDR chunk: length (4 bytes), 'IHDR' (4 bytes), width (4 bytes = 100), height (4 bytes = 200)
    png_header = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x64"  # width 100
        b"\x00\x00\x00\xc8"  # height 200
        b"\x08\x06\x00\x00\x00\x00\x00\x00\x00"
    )
    img_file = tmp_path / "test_image.png"
    img_file.write_bytes(png_header)

    w, h = detect_image_dimensions(png_header, "image/png")
    assert w == 100
    assert h == 200

    param, err = parse_and_attach_image(str(img_file), str(tmp_path))
    assert err is None
    assert param is not None
    assert param["type"] == "image_url"
    assert param["image_url"]["url"].startswith("data:image/png;base64,")
    assert param["width"] == 100
    assert param["height"] == 200

    # Non-existent file
    param_fail, err_fail = parse_and_attach_image("missing.jpg", str(tmp_path))
    assert param_fail is None
    assert "not found" in err_fail


def test_session_pagination_and_search(tmp_path: pathlib.Path):
    """Point 11: Ensure sessions menu supports pagination and search without hard 15 cutoff."""
    # Create 25 mock sessions
    mock_sessions = [
        SessionEntry(
            id=f"sess_{i:03d}",
            status="completed",
            summary=f"Task number {i}" + (" auth feature" if i == 5 else ""),
            active_tokens=100 * i,
            plan_mode=(i % 2 == 0),
        )
        for i in range(1, 26)
    ]

    # Test selecting session 1 by number
    with patch("builtins.input", return_value="1"):
        res = select_session_interactive(None, mock_sessions)
        assert res == "sess_001"

    # Test pagination: 'n' advances then select 11
    inputs = iter(["n", "11"])
    with patch("builtins.input", side_effect=lambda _: next(inputs)):
        res = select_session_interactive(None, mock_sessions)
        assert res == "sess_011"

    # Test search filter: 's auth' filters list down to 1 item, then select 1
    inputs_search = iter(["s auth", "1"])
    with patch("builtins.input", side_effect=lambda _: next(inputs_search)):
        res = select_session_interactive(None, mock_sessions)
        assert res == "sess_005"


def test_session_rename(tmp_path: pathlib.Path):
    """Point 13: Ensure rename_session in SessionManager."""
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-5.6-luna"},
        get_resolved_settings=lambda: {"model": "gpt-5.6-luna"},
    )
    session_id = "test_rename_sess_001"
    mgr._save_index(
        {
            "version": 1,
            "entries": [
                {
                    "id": session_id,
                    "summary": "Original Title",
                    "active_tokens": 500,
                    "plan_mode": False,
                }
            ],
        }
    )

    # Rename session
    success = mgr.rename_session(session_id, "Updated New Feature Title")
    assert success is True

    entry = mgr.get_session(session_id)
    assert entry is not None
    assert entry.summary == "Updated New Feature Title"

    # Invalid / empty title
    assert mgr.rename_session(session_id, "   ") is False
    # Non-existent session
    assert mgr.rename_session("missing_sess_id", "Title") is False


def test_lsp_and_teams_execution(tmp_path: pathlib.Path, capsys):
    """Ensure /lsp and /teams render status cleanly without import error."""
    from coderai.core.lsp.client import LSP_SERVER_COMMANDS, get_lsp_client
    from coderai.core.teams.manager import TeamManager

    lsp_client = get_lsp_client(str(tmp_path))
    assert lsp_client is not None
    assert len(LSP_SERVER_COMMANDS) >= 5

    team_mgr = TeamManager()
    tm = team_mgr.spawn_teammate("architect", "System Architect")
    assert tm.name == "architect"
    teammates = team_mgr.list_teammates()
    assert len(teammates) == 1

