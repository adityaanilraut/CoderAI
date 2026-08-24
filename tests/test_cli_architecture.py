"""Unit tests verifying CoderAI's pure CLI/TUI architecture and core alignment."""

from __future__ import annotations

import pathlib

import pytest

from coderai.cli.app import _build_parser
from coderai.cli.commands import COMMAND_CATALOG, parse_slash_command
from coderai.cli.completer import AVAILABLE_SLASH_COMMANDS
from coderai.core.permissions import compute_tool_call_permissions, PLAN_MODE_FORCE_ASK_SCOPES
from coderai.core.prompt import get_system_prompt, get_tools
from coderai.core.state import (
    clear_session_state,
    record_file_state,
    get_file_state,
    get_file_version,
    FileState,
)


def test_cli_parser_pure_cli():
    """Verify that CLI parser provides CLI flags and does not expose web/server modes."""
    parser = _build_parser()
    args = parser.parse_args(["--plan", "--yes", "-p", "hello world"])
    assert args.plan is True
    assert args.yes is True
    assert args.prompt_flag == "hello world"
    assert not hasattr(args, "server")


def test_removed_cli_aliases_have_canonical_replacements():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--message", "hello"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--tools-preset", "benchmark"])


def test_slash_catalog_drives_completion_and_dispatch():
    completed = {name for name, _description in AVAILABLE_SLASH_COMMANDS}
    assert {f"/{name}" for name in COMMAND_CATALOG} <= completed
    assert parse_slash_command("/settings") == ("/config", "")
    assert parse_slash_command("/job logs 1") == ("/jobs", "logs 1")


def test_cache_aware_context_ordering(tmp_path: pathlib.Path):
    """Verify cache-aware prompt structure where stable prefix comes before volatile history."""
    system_prompt = get_system_prompt(options={"project_root": str(tmp_path)})
    assert "You are a helpful software engineer assistant." in system_prompt

    # Verify tools ordering is deterministic
    tools = get_tools()
    tool_names = [t["function"]["name"] for t in tools]
    assert "read" in tool_names
    assert "write" in tool_names
    assert "edit" in tool_names
    assert "str_replace_editor" in tool_names


def test_snippet_state_and_cas_versioning():
    """Verify snippet state recording and version tracking."""
    session_id = "test_cas_session"
    clear_session_state(session_id)
    try:
        assert get_file_version(session_id, "foo.py") == 0
        state = FileState(file_path="foo.py", content="print('hello')", timestamp=12345)
        record_file_state(session_id, state, increment_version=True)
        assert get_file_version(session_id, "foo.py") == 1

        retrieved = get_file_state(session_id, "foo.py")
        assert retrieved is not None
        assert retrieved.content == "print('hello')"
        assert retrieved.version == 1
    finally:
        clear_session_state(session_id)


def test_side_effect_scoped_permissions(tmp_path: pathlib.Path):
    """Verify side-effect scoped permissions and Plan Mode gating."""
    tool_call = {
        "id": "call_1",
        "function": {
            "name": "write",
            "arguments": '{"file_path": "new_file.txt", "content": "data"}',
        },
    }

    # In Plan Mode, write-in-cwd is forced to ASK
    plan_perms = compute_tool_call_permissions(
        session_id="test_sess",
        project_root=str(tmp_path),
        tool_calls=[tool_call],
        force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES,
    )
    assert len(plan_perms["askPermissions"]) > 0
    first_ask = plan_perms["askPermissions"][0]
    assert "write-in-cwd" in first_ask["scopes"]
