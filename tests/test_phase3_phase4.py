"""Unit tests for Phase 3 (Sessions, Persistence & Recovery) and Phase 4 (Agent Loop, MCP & Extensibility)."""

import json
import pathlib
import pytest

from coderai.core.mcp.manager import McpManager
from coderai.core.prompt import format_tool_definitions
from coderai.core.session import SessionManager, get_project_code
from coderai.core.settings import (
    first_token_window,
    get_default_auto_compact_window,
    get_default_context_window,
    parse_token_window,
)
from coderai.core.state import (
    FileState,
    get_file_version,
    get_snippet,
    has_session_state,
    record_file_state,
)
from coderai.core.subagent import MAX_SUBAGENT_DEPTH, SubAgentManager, SubAgentSpec
from coderai.core.tools.subagent import handle_subagent_tool
from coderai.core.tools.types import ToolExecutionContext


def _resp(content: str = "ok", tool_calls=None, usage=None):
    choice = {"message": {"content": content, "tool_calls": tool_calls, "refusal": None}}
    return {
        "choices": [choice],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


# ============================================================================
# Phase 3.1: Project-Local Session Storage & Migration
# ============================================================================


def test_parse_token_window_helpers():
    assert parse_token_window(1000) == 1000
    assert parse_token_window(1000.0) == 1000
    assert parse_token_window(False) is None
    assert parse_token_window(None) is None
    assert parse_token_window("128k") == 128 * 1024
    assert parse_token_window("64K") == 64 * 1024
    assert parse_token_window("1m") == 1024 * 1024
    assert parse_token_window("2M") == 2 * 1024 * 1024
    assert parse_token_window("500000") == 500000
    assert parse_token_window("invalid") is None
    assert first_token_window(None, "", "256k") == 256 * 1024
    assert get_default_context_window("deepseek-v4-pro") == 1024 * 1024
    assert get_default_auto_compact_window("deepseek-v4-pro") == 512 * 1024


def test_project_local_session_storage_primary(tmp_path: pathlib.Path):
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()

    mgr = SessionManager(
        project_root=str(project_dir),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {},
    )

    storage = mgr._storage()
    expected_local = project_dir / ".coderai" / "sessions"
    assert storage["project_dir"] == expected_local
    assert storage["index_path"] == expected_local / "sessions-index.json"
    assert expected_local.exists()


def test_storage_migration_from_global_to_local(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    project_dir = tmp_path / "migrated_project"
    project_dir.mkdir()

    code = get_project_code(str(project_dir))
    global_project_dir = home_dir / ".coderai" / "projects" / code
    global_project_dir.mkdir(parents=True, exist_ok=True)

    # Populate global storage with legacy session data
    global_index = {
        "version": 1,
        "entries": [
            {
                "id": "legacy_session_123",
                "summary": "Legacy session before migration",
                "status": "completed",
                "activeTokens": 50,
            }
        ],
        "originalPath": str(project_dir),
    }
    (global_project_dir / "sessions-index.json").write_text(
        json.dumps(global_index), encoding="utf-8"
    )
    (global_project_dir / "legacy_session_123.jsonl").write_text(
        json.dumps(
            {"id": "msg1", "sessionId": "legacy_session_123", "role": "user", "content": "hello"}
        )
        + "\n",
        encoding="utf-8",
    )
    (global_project_dir / "images" / "legacy_session_123").mkdir(parents=True, exist_ok=True)
    (global_project_dir / "images" / "legacy_session_123" / "test.png").write_bytes(b"\x89PNG")

    # Initializing SessionManager should auto-migrate files into project-local .coderai/sessions/
    mgr = SessionManager(
        project_root=str(project_dir),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {},
    )

    local_dir = project_dir / ".coderai" / "sessions"
    assert local_dir.exists()
    assert (local_dir / "sessions-index.json").exists()
    assert (local_dir / "legacy_session_123.jsonl").exists()
    assert (local_dir / "images" / "legacy_session_123" / "test.png").exists()

    loaded_session = mgr.get_session("legacy_session_123")
    assert loaded_session is not None
    assert loaded_session.summary == "Legacy session before migration"


# ============================================================================
# Phase 3.2: Active Token Calculation & Auto-Compaction Window
# ============================================================================


@pytest.mark.asyncio
async def test_active_token_calculation_and_auto_compaction(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    calls = []

    def script(call_list, kwargs):
        msgs = kwargs.get("messages", [])
        if any("skillNames" in str(m.get("content", "")) for m in msgs):
            return _resp('{"skillNames": []}')
        call_list.append(kwargs)
        if len(call_list) == 1:
            # Turn 1 response with 100 total tokens
            return _resp(
                "Turn 1 response",
                usage={"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100},
            )
        elif len(call_list) == 2:
            # Compaction call triggered because autoCompactWindow is 50
            return _resp(
                "<analysis>Analysis</analysis>Compacted summary of previous conversation.",
                usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
            )
        else:
            # Turn 2 response
            return _resp(
                "Turn 2 response",
                usage={"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
            )

    class Completions:
        def create(self, **kwargs):
            return script(calls, kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "autoCompactWindow": 50,  # low threshold to trigger auto-compaction on next turn
            "enabledSkills": {
                "image-generator": False,
                "skill-digester": False,
                "skill-writer": False,
                "coderai-self-refer": False,
            },
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    # Turn 1
    session_id = await mgr.create_session("First prompt")
    entry = mgr.get_session(session_id)
    assert entry is not None
    assert entry.active_tokens == 100

    # Turn 2: activeTokens (100) > autoCompactWindow (50), so compaction triggers before Turn 2 completion
    await mgr.reply_session(session_id, "Second prompt")

    # Should have called LLM for Turn 1, Compaction, and Turn 2
    assert len(calls) == 3

    # Verify messages list has summary message inserted and earlier messages compacted
    messages = mgr.list_session_messages(session_id)
    from coderai.core.session_log import derive_messages

    derived = derive_messages(messages)
    assert len(derived) < len(messages)
    summary_messages = [
        m for m in messages if m.role == "system" and "There are earlier parts" in m.content
    ]
    assert len(summary_messages) == 1


# ============================================================================
# Phase 3.3: State Cleanup on Undo & Interrupt & Delete
# ============================================================================


@pytest.mark.asyncio
async def test_session_undo_clears_state_and_rebuilds(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    target_file = tmp_path / "hello.txt"
    target_file.write_text("line 1\nline 2\nline 3\n")

    calls = []

    def script(call_list, kwargs):
        msgs = kwargs.get("messages", [])
        if any("skillNames" in str(m.get("content", "")) for m in msgs):
            return _resp('{"skillNames": []}')
        call_list.append(kwargs)
        if len(call_list) == 1:
            # Execute read tool
            return _resp(
                "Reading file.",
                tool_calls=[
                    {
                        "id": "tc_read_1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"file_path": str(target_file)}),
                        },
                    }
                ],
            )
        elif len(call_list) == 2:
            return _resp("Done read turn.")
        elif len(call_list) == 3:
            # Second user prompt: edit file
            snippet = get_snippet(
                mgr._active_session_id,
                f"full_file_{get_file_version(mgr._active_session_id, str(target_file))}",
            )
            snip_id = snippet.id if snippet else "full_file_1"
            return _resp(
                "Editing file.",
                tool_calls=[
                    {
                        "id": "tc_edit_1",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {
                                    "snippet_id": snip_id,
                                    "old_string": "line 2",
                                    "new_string": "line 2 modified",
                                }
                            ),
                        },
                    }
                ],
            )
        return _resp("Done edit turn.")

    class Completions:
        def create(self, **kwargs):
            return script(calls, kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "enabledSkills": {
                "image-generator": False,
                "skill-digester": False,
                "skill-writer": False,
                "coderai-self-refer": False,
            },
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    # Turn 1: read
    session_id = await mgr.create_session("Read the file")
    assert has_session_state(session_id)

    # Turn 2: edit
    await mgr.reply_session(session_id, "Edit line 2")
    assert target_file.read_text() == "line 1\nline 2 modified\nline 3\n"

    # Now Undo Turn 2
    undone = mgr.undo(session_id)
    assert undone is True
    # File content restored
    assert target_file.read_text() == "line 1\nline 2\nline 3\n"

    # Session state was cleared and rebuilt
    assert has_session_state(session_id)


def test_session_delete_cleans_images_and_state(tmp_path: pathlib.Path):
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {},
    )

    session_id = "test_del_session"
    record_file_state(session_id, FileState(file_path="foo.txt", content="foo", timestamp=100))
    assert has_session_state(session_id)

    # Create session images dir
    img_dir = mgr._storage()["project_dir"] / "images" / session_id
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "capture.png").write_bytes(b"PNG")

    # Add entry to index
    index = mgr._load_index()
    index["entries"].append({"id": session_id, "summary": "Session to delete"})
    mgr._save_index(index)

    # Delete session
    deleted = mgr.delete_session(session_id)
    assert deleted is True
    assert not has_session_state(session_id)
    assert not img_dir.exists()


# ============================================================================
# Phase 4: Dynamic Tool Schema, MCP Dynamic Sync & Subagent Recursion Guard
# ============================================================================


def test_format_tool_definitions_strict_schemas():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "test_fn",
                "description": "Test function",
                "parameters": {
                    "type": "object",
                    "properties": {"arg1": {"type": "string"}},
                    "required": ["arg1"],
                },
            },
        }
    ]

    formatted_strict = format_tool_definitions(tools, model="gpt-4o", strict=True)
    assert len(formatted_strict) == 1
    fn = formatted_strict[0]["function"]
    assert fn["strict"] is True
    assert fn["parameters"]["additionalProperties"] is False

    formatted_lenient = format_tool_definitions(tools, model="claude-3-haiku", strict=False)
    assert len(formatted_lenient) == 1
    assert "strict" not in formatted_lenient[0]["function"]


@pytest.mark.asyncio
async def test_mcp_manager_dynamic_sync(tmp_path: pathlib.Path):
    mcp = McpManager()

    # Initial empty sync
    await mcp.sync_servers({})
    assert len(mcp.get_status()) == 0

    tools_changed = []
    mcp.set_on_tools_list_changed(lambda: tools_changed.append(True))

    # Add mock server
    mock_servers = {
        "test_srv": {
            "command": "echo",
            "args": ["hello"],
        }
    }
    await mcp.sync_servers(mock_servers)
    assert "test_srv" in mcp.configured_server_names

    # Remove server dynamically
    await mcp.sync_servers({})
    assert "test_srv" not in mcp.configured_server_names
    assert len(mcp.clients) == 0
    assert len(tools_changed) > 0


@pytest.mark.asyncio
async def test_subagent_recursion_guard_and_parent_linking(tmp_path: pathlib.Path):
    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": None,
            "model": "gpt-4o",
        },
    )

    # Recursion limit exceeded
    spec = SubAgentSpec(
        description="Deep nested agent",
        prompt="Do recursive work",
        depth=MAX_SUBAGENT_DEPTH + 1,
        parent_session_id="parent123456",
    )

    res = await manager.spawn_subagent(spec)
    assert res.status == "failed"
    assert "nesting depth exceeded" in res.summary.lower()
    assert "parent12" in res.session_id


@pytest.mark.asyncio
async def test_subagent_task_tool_passes_depth_and_parent(tmp_path: pathlib.Path):
    called_specs = []

    async def mock_spawn(spec: SubAgentSpec):
        called_specs.append(spec)
        from coderai.core.subagent import SubAgentResult

        return SubAgentResult(
            task_id=spec.task_id,
            session_id=f"sub_{spec.parent_session_id}_{spec.task_id}",
            status="completed",
            summary="Sub-agent finished.",
        )

    context = ToolExecutionContext(
        project_root=str(tmp_path),
        session_id="sess_parent_789",
        create_openai_client=lambda: {"client": type("C", (), {"chat": None})(), "model": "gpt-4o"},
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SubAgentManager, "spawn_subagent", lambda self, s: mock_spawn(s))

        res = await handle_subagent_tool(
            {
                "description": "Analyze codebase",
                "prompt": "Find all functions",
                "depth": 0,
            },
            context,
        )

        assert res.ok is True
        assert len(called_specs) == 1
        assert called_specs[0].parent_session_id == "sess_parent_789"
        assert called_specs[0].depth == 0
