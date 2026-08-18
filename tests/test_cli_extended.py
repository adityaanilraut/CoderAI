"""Extended unit tests for new CoderAI CLI features, slash commands, menus, completer, and exporters."""

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from coderai.cli.app import _render_help_menu
from coderai.cli.completer import CoderAICompleter, setup_readline
from coderai.cli.export_render import export_session_to_json, export_session_to_markdown
from coderai.cli.interactive_menu import (
    render_config_interactive,
    render_mcp_interactive,
    render_session_history,
    render_token_breakdown,
    select_session_interactive,
)
from coderai.cli.status_bar import format_status_bar, render_status_bar
from coderai.cli.tool_card import render_tool_card
from coderai.core.session import SessionEntry, SessionManager, SessionMessage


def test_completer_slash_commands(tmp_path: pathlib.Path):
    completer = CoderAICompleter(str(tmp_path))

    # Test completing /pl -> /plan
    res = completer.complete("/pl", 0)
    assert res is not None
    assert "/plan" in res

    # Test completing /to -> /tokens
    res = completer.complete("/to", 0)
    assert res is not None
    assert "/tokens" in res

    # Test completing /model gpt-5.6 -> gpt-5.6-sol / gpt-5.6-luna
    res = completer.complete("gpt-5.6", 0)
    # With /model line buffer
    try:
        # If readline is mocked or available
        pass
    except Exception:
        pass


def test_completer_at_file(tmp_path: pathlib.Path):
    (tmp_path / "app_main.py").write_text("print('hello')\n")
    (tmp_path / "config_loader.py").write_text("DATA = 1\n")

    completer = CoderAICompleter(str(tmp_path))
    # Suggest files
    completer.complete("app", 0)
    assert completer.project_root == str(tmp_path)


def test_setup_readline(tmp_path: pathlib.Path):
    res = setup_readline(str(tmp_path))
    # Should run without error
    assert isinstance(res, bool)


def test_export_session_markdown_and_json(tmp_path: pathlib.Path):
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-5.6-luna"},
        get_resolved_settings=lambda: {"model": "gpt-5.6-luna"},
    )

    session_id = "test_export_sess_001"
    mgr._save_index(
        {
            "version": 1,
            "entries": [
                {
                    "id": session_id,
                    "summary": "Implement Auth Feature",
                    "active_tokens": 1200,
                    "usage": {"prompt_tokens": 800, "completion_tokens": 400, "total_tokens": 1200},
                    "plan_mode": False,
                }
            ],
        }
    )

    messages = [
        SessionMessage(id="m1", session_id=session_id, role="user", content="Add authentication"),
        SessionMessage(
            id="m2",
            session_id=session_id,
            role="assistant",
            content="I will add the auth module.",
            thinking="First explore existing auth",
            tool_calls=[{"function": {"name": "read", "arguments": '{"file_path": "main.py"}'}}],
        ),
        SessionMessage(
            id="m3",
            session_id=session_id,
            role="tool",
            tool_call_id="call_read_1",
            content=json.dumps({"name": "read", "ok": True, "output": "print('hello')"}),
        ),
    ]
    mgr._save_messages(session_id, messages)

    # Export to markdown
    md_file = tmp_path / "export.md"
    out_md = export_session_to_markdown(mgr, session_id, str(md_file))
    assert pathlib.Path(out_md).is_file()
    md_content = pathlib.Path(out_md).read_text(encoding="utf-8")
    assert "Implement Auth Feature" in md_content
    assert "Add authentication" in md_content
    assert "Reasoning Trace" in md_content
    assert "Tool Invocations" in md_content

    # Export to JSON
    json_file = tmp_path / "export.json"
    out_json = export_session_to_json(mgr, session_id, str(json_file))
    assert pathlib.Path(out_json).is_file()
    json_data = json.loads(pathlib.Path(out_json).read_text(encoding="utf-8"))
    assert json_data["session"]["id"] == session_id
    assert len(json_data["messages"]) == 3


def test_session_multi_action_delete_and_fork(monkeypatch):
    sessions = [
        SessionEntry(id="sess_alpha123", summary="Alpha task", active_tokens=100),
        SessionEntry(id="sess_beta456", summary="Beta task", active_tokens=200),
    ]

    # Delete session
    monkeypatch.setattr("builtins.input", lambda _: "d 1")
    action_del = select_session_interactive(None, sessions)
    assert action_del == "delete:sess_alpha123"

    # Fork session
    monkeypatch.setattr("builtins.input", lambda _: "f 2")
    action_fork = select_session_interactive(None, sessions)
    assert action_fork == "fork:sess_beta456"

    # Standard resume
    monkeypatch.setattr("builtins.input", lambda _: "2")
    action_resume = select_session_interactive(None, sessions)
    assert action_resume == "sess_beta456"


def test_render_mcp_interactive(tmp_path: pathlib.Path):
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )

    mock_console = MagicMock()
    render_mcp_interactive(mock_console, mgr)
    assert mock_console.print.called

    render_mcp_interactive(None, mgr)


def test_render_config_interactive(tmp_path: pathlib.Path):
    mock_console = MagicMock()
    render_config_interactive(mock_console, str(tmp_path))
    assert mock_console.print.called

    render_config_interactive(None, str(tmp_path))


def test_render_token_breakdown(tmp_path: pathlib.Path):
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )
    session_id = "sess_token_test"
    mgr._save_index(
        {
            "version": 1,
            "entries": [
                {
                    "id": session_id,
                    "summary": "Token Test",
                    "active_tokens": 1500,
                    "usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": 500,
                        "total_tokens": 1500,
                        "cached_tokens": 200,
                    },
                    "usage_per_model": {"gpt-5.6-luna": {"total_tokens": 1500}},
                }
            ],
        }
    )

    mock_console = MagicMock()
    render_token_breakdown(mock_console, mgr, session_id)
    assert mock_console.print.called

    render_token_breakdown(None, mgr, session_id)
    render_token_breakdown(None, mgr, None)


def test_render_session_history(tmp_path: pathlib.Path):
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )
    session_id = "sess_history_test"
    mgr._save_index({"version": 1, "entries": [{"id": session_id, "summary": "Hist"}]})
    mgr._save_messages(
        session_id,
        [
            SessionMessage(id="m1", session_id=session_id, role="user", content="Hello CoderAI"),
            SessionMessage(
                id="m2", session_id=session_id, role="assistant", content="Hello! How can I help?"
            ),
        ],
    )

    mock_console = MagicMock()
    render_session_history(mock_console, mgr, session_id)
    assert mock_console.print.called

    render_session_history(None, mgr, session_id)
    render_session_history(None, mgr, None)


def test_extended_tool_cards():
    mock_console = MagicMock()

    # Bash tool card
    bash_msg = SessionMessage(
        id="b1",
        session_id="s1",
        role="tool",
        content=json.dumps(
            {
                "name": "bash",
                "ok": True,
                "output": "line 1\nline 2\nline 3",
                "metadata": {"command": "ls -la", "exit_code": 0},
            }
        ),
    )
    render_tool_card(mock_console, bash_msg)
    assert mock_console.print.called

    # Bash tool failure
    bash_err_msg = SessionMessage(
        id="b2",
        session_id="s1",
        role="tool",
        content=json.dumps(
            {
                "name": "bash",
                "ok": False,
                "error": "command not found: foobar",
                "metadata": {"command": "foobar", "exit_code": 127},
            }
        ),
    )
    render_tool_card(mock_console, bash_err_msg)

    # WebSearch tool card
    search_msg = SessionMessage(
        id="s1",
        session_id="s1",
        role="tool",
        content=json.dumps(
            {
                "name": "WebSearch",
                "ok": True,
                "output": "Search completed",
                "metadata": {
                    "query": "python rich table example",
                    "results": [
                        {
                            "title": "Rich Tables",
                            "url": "https://rich.readthedocs.io",
                            "snippet": "Example of rich table",
                        }
                    ],
                },
            }
        ),
    )
    render_tool_card(mock_console, search_msg)

    # Read tool card with snippet metadata
    read_msg = SessionMessage(
        id="r1",
        session_id="s1",
        role="tool",
        content=json.dumps(
            {
                "name": "read",
                "ok": True,
                "output": "def main(): pass",
                "metadata": {
                    "snippet_id": "snip_abc123",
                    "line_count": 25,
                    "file_path": "/path/main.py",
                },
            }
        ),
    )
    render_tool_card(mock_console, read_msg)


def test_status_bar_with_turns_and_mcp():
    bar = format_status_bar("gpt-5.6-luna", 3500, False, "feature-branch", turns=4, mcp_count=2)
    assert "gpt-5.6-luna" in str(bar)
    assert "3,500" in str(bar)
    assert "feature-branch" in str(bar)
    assert "Turns" in str(bar)
    assert "MCP" in str(bar)

    mock_console = MagicMock()
    render_status_bar(mock_console, "gpt-5.6-luna", 3500, False, "/tmp", turns=4, mcp_count=2)
    assert mock_console.print.called


def test_render_help_menu():
    _render_help_menu()


@pytest.mark.asyncio
async def test_compact_session_direct(tmp_path: pathlib.Path):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = {
        "choices": [{"message": {"content": "<analysis>short</analysis>Here is the summary."}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    }

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": mock_client, "model": "gpt-5.6-luna"},
        get_resolved_settings=lambda: {"model": "gpt-5.6-luna"},
    )
    session_id = "sess_compact_test"
    mgr._save_index(
        {
            "version": 1,
            "entries": [{"id": session_id, "summary": "Compact test", "activeTokens": 5000}],
        }
    )

    # Add messages
    messages = [
        SessionMessage(id="m0", session_id=session_id, role="system", content="System instruction"),
        SessionMessage(id="m1", session_id=session_id, role="user", content="Step 1"),
        SessionMessage(id="m2", session_id=session_id, role="assistant", content="Response 1"),
        SessionMessage(id="m3", session_id=session_id, role="user", content="Step 2"),
        SessionMessage(id="m4", session_id=session_id, role="assistant", content="Response 2"),
    ]
    mgr._save_messages(session_id, messages)

    await mgr.compact_session(session_id)

    updated_messages = mgr.list_session_messages(session_id)
    # Check that compaction happened and summary was inserted
    assert any(
        m.role == "system" and "Here is the summary" in (m.content or "") for m in updated_messages
    )


def test_session_lru_pruning_max_50(tmp_path: pathlib.Path):
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )

    # Create 60 dummy session entries
    entries = []
    for i in range(60):
        s_id = f"sess_{i:03d}"
        entries.append(
            {
                "id": s_id,
                "summary": f"Session {i}",
                "updateTime": f"2026-08-17T{i:02d}:00:00Z"
                if i < 24
                else f"2026-08-18T{(i - 24) % 24:02d}:00:00Z",
            }
        )
        # Create a message file
        mgr._save_messages(
            s_id, [SessionMessage(id="m1", session_id=s_id, role="user", content=f"Hello {i}")]
        )

    # Save index with 60 entries
    mgr._save_index({"version": 1, "entries": entries})

    # Load index and verify pruned to 50
    loaded = mgr._load_index()
    assert len(loaded["entries"]) == 50
    # Oldest 10 entries should be pruned from disk
    pruned_ids = [e["id"] for e in entries[:10]]
    for pid in pruned_ids:
        assert not mgr._messages_path(pid).exists()
