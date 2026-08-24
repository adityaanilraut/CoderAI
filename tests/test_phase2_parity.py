"""Tests for Phase 2: Workflow & Multi-Target Parity.

Covers:
1. Interactive PlanImplementationPrompt & post-plan decision flow
2. Plan Mode Forced Mutating Scopes (PLAN_MODE_FORCE_ASK_SCOPES)
3. Multi-Target Interactive /undo (list_undo_targets + 3 restore modes)
4. External Skill Scan Paths (.claude/skills, .agents/skills, custom paths)
5. Interactive MCP Reconnect, Prompts, Resources, and CLI
"""

from __future__ import annotations

import json
import pathlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from coderai.cli.app import _prompt_permissions
from coderai.cli.completer import CoderAICompleter
from coderai.cli.interactive_menu import (
    prompt_plan_implementation,
    select_undo_interactive,
)
from coderai.core.mcp.client import McpClient
from coderai.core.mcp.manager import McpManager
from coderai.core.permissions import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    evaluate_permission_scopes,
    get_scopes_requiring_ask,
)
from coderai.core.skill import get_skill_scan_roots, list_skills, load_skill
from coderai.core.session import SessionManager
from coderai.core.settings import resolve_current_settings
from coderai.core.state import get_file_version, get_snippet


# ============================================================================
# 1. Plan Mode Forced Mutating Scopes
# ============================================================================


def test_plan_mode_forced_mutating_scopes():
    """Verify PLAN_MODE_FORCE_ASK_SCOPES forces 'ask' even when allowAll or allowed explicitly."""
    # Default allowAll settings
    permissive_settings = {
        "allow": ["write-in-cwd", "delete-in-cwd"],
        "deny": [],
        "ask": [],
        "defaultMode": "allowAll",
    }

    # Normal mode: write-in-cwd is allowed
    decision_normal = evaluate_permission_scopes(
        ["write-in-cwd"], settings=permissive_settings, force_ask_scopes=None
    )
    assert decision_normal == "allow"

    # Plan mode: write-in-cwd is forced to ask
    decision_plan = evaluate_permission_scopes(
        ["write-in-cwd"], settings=permissive_settings, force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES
    )
    assert decision_plan == "ask"

    # Plan mode: read-in-cwd remains allowed
    decision_read = evaluate_permission_scopes(
        ["read-in-cwd"], settings=permissive_settings, force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES
    )
    assert decision_read == "allow"

    # Scopes requiring ask
    ask_scopes = get_scopes_requiring_ask(
        ["write-in-cwd", "read-in-cwd"],
        settings=permissive_settings,
        force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES,
    )
    assert "write-in-cwd" in ask_scopes
    assert "read-in-cwd" not in ask_scopes


def test_prompt_permissions_intercepts_in_plan_mode():
    """_prompt_permissions with yes=True must NOT auto-approve forced mutating scopes when plan_mode=True."""
    requests = [
        {
            "toolCallId": "tc_write_1",
            "name": "write",
            "command": "write test.txt",
            "scopes": ["write-in-cwd"],
        }
    ]

    # In non-plan mode with yes=True, it is auto-allowed
    replies, always = _prompt_permissions(requests, yes=True, plan_mode=False)
    assert len(replies) == 1
    assert replies[0]["permission"] == "allow"
    assert "write-in-cwd" in always

    # In plan mode with yes=True, mutating scope cannot be auto-allowed silently
    with patch("builtins.input", return_value="3"):  # User denies
        replies_plan, _ = _prompt_permissions(requests, yes=True, plan_mode=True)
        assert len(replies_plan) == 1
        assert replies_plan[0]["permission"] == "deny"


# ============================================================================
# 2. PlanImplementationPrompt
# ============================================================================


def test_prompt_plan_implementation_choices():
    """Test interactive plan implementation decision choices."""
    with patch("builtins.input", return_value="1"):
        assert prompt_plan_implementation(None) == "execute"

    with patch("builtins.input", return_value="y"):
        assert prompt_plan_implementation(None) == "execute"

    with patch("builtins.input", return_value=""):
        assert prompt_plan_implementation(None) == "execute"

    with patch("builtins.input", return_value="2"):
        assert prompt_plan_implementation(None) == "refine"

    with patch("builtins.input", return_value="r"):
        assert prompt_plan_implementation(None) == "refine"

    with patch("builtins.input", return_value="3"):
        assert prompt_plan_implementation(None) == "stay"


# ============================================================================
# 3. External Skill Scan Paths
# ============================================================================


def test_external_skill_scan_paths_discovery(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify skills are discovered in .claude/skills, .agents/skills, and custom scan paths."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Create skill in ~/.claude/skills
    claude_skills = home / ".claude" / "skills" / "claude-tool"
    claude_skills.mkdir(parents=True)
    (claude_skills / "SKILL.md").write_text(
        "---\nname: claude-tool\ndescription: Claude compatibility skill\n---\n# Instructions\nDo Claude stuff.\n"
    )

    # Create skill in project/.agents/skills
    proj_agent_skills = tmp_path / ".agents" / "skills" / "agent-skill"
    proj_agent_skills.mkdir(parents=True)
    (proj_agent_skills / "SKILL.md").write_text(
        "---\nname: agent-skill\ndescription: Agent helper skill\n---\n# Instructions\nDo Agent stuff.\n"
    )

    # Create skill in custom scan path
    custom_dir = tmp_path / "custom_skills" / "custom-tool"
    custom_dir.mkdir(parents=True)
    (custom_dir / "SKILL.md").write_text(
        "---\nname: custom-tool\ndescription: Custom scan path skill\n---\n# Instructions\nDo custom stuff.\n"
    )

    roots = get_skill_scan_roots(str(tmp_path), custom_scan_paths=[str(tmp_path / "custom_skills")])
    root_paths = [r[0] for r in roots]

    assert str(tmp_path / ".agents" / "skills") in root_paths
    assert str(tmp_path / ".claude" / "skills") in root_paths
    assert str(home / ".claude" / "skills") in root_paths
    assert str(tmp_path / "custom_skills") in root_paths

    skills = list_skills(str(tmp_path), custom_scan_paths=[str(tmp_path / "custom_skills")])
    skill_names = {s["name"] for s in skills}

    assert "claude-tool" in skill_names
    assert "agent-skill" in skill_names
    assert "custom-tool" in skill_names

    loaded = load_skill("claude-tool", str(tmp_path))
    assert loaded is not None
    assert "Do Claude stuff." in loaded["instructions"]


def test_settings_skill_scan_paths_merge(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Verify settings merge skillScanPaths correctly."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # User settings
    (home / ".coderai").mkdir(parents=True)
    (home / ".coderai" / "settings.json").write_text(
        json.dumps({"skillScanPaths": ["/opt/global/skills"]})
    )

    # Project settings
    (tmp_path / ".coderai").mkdir(parents=True)
    (tmp_path / ".coderai" / "settings.json").write_text(
        json.dumps({"skillScanPaths": ["./team_skills"]})
    )

    settings = resolve_current_settings(str(tmp_path))
    paths = settings.get("skillScanPaths") or []
    assert "/opt/global/skills" in paths
    assert "./team_skills" in paths


# ============================================================================
# 4. Multi-Target Interactive /undo & 3 Restore Modes
# ============================================================================


def _resp(content: str = "ok", tool_calls=None, usage=None):
    choice = {"message": {"content": content, "tool_calls": tool_calls, "refusal": None}}
    return {
        "choices": [choice],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_list_undo_targets_and_modes(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Test list_undo_targets, restore_both, restore_conversation_only, and restore_code_only."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    test_file = tmp_path / "code.py"
    test_file.write_text("line 1\nline 2\nline 3\n")

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
                            "arguments": json.dumps({"file_path": str(test_file)}),
                        },
                    }
                ],
            )
        elif len(call_list) == 2:
            return _resp("Done read turn.")
        elif len(call_list) == 3:
            # Turn 2: edit line 2 to line 2 modified
            snippet = get_snippet(
                mgr._active_session_id,
                f"full_file_{get_file_version(mgr._active_session_id, str(test_file))}",
            )
            snip_id = snippet.id if snippet else "full_file_1"
            return _resp(
                "Editing file v1.",
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
                                    "new_string": "line 2 v1",
                                }
                            ),
                        },
                    }
                ],
            )
        elif len(call_list) == 4:
            return _resp("Done turn 2.")
        elif len(call_list) == 5:
            # Turn 3: read file again before editing
            return _resp(
                "Reading file before Turn 3 edit.",
                tool_calls=[
                    {
                        "id": "tc_read_2",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"file_path": str(test_file)}),
                        },
                    }
                ],
            )
        elif len(call_list) == 6:
            # Turn 3: edit line 3 to line 3 v2
            snippet = get_snippet(
                mgr._active_session_id,
                f"full_file_{get_file_version(mgr._active_session_id, str(test_file))}",
            )
            snip_id = snippet.id if snippet else "full_file_2"
            return _resp(
                "Editing file v2.",
                tool_calls=[
                    {
                        "id": "tc_edit_2",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {
                                    "snippet_id": snip_id,
                                    "old_string": "line 3",
                                    "new_string": "line 3 v2",
                                }
                            ),
                        },
                    }
                ],
            )
        return _resp("Done turn 3.")

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
    session_id = await mgr.create_session("Turn 1: Initial read")
    # Turn 2: edit line 2
    await mgr.reply_session(session_id, "Turn 2: Edit line 2")
    assert test_file.read_text() == "line 1\nline 2 v1\nline 3\n"

    # Turn 3: edit line 3
    await mgr.reply_session(session_id, "Turn 3: Edit line 3")
    assert test_file.read_text() == "line 1\nline 2 v1\nline 3 v2\n"

    targets = mgr.list_undo_targets(session_id)
    assert len(targets) == 3
    assert targets[0]["turn_index"] == 1
    assert "Turn 1" in targets[0]["prompt"]
    assert targets[1]["turn_index"] == 2
    assert "Turn 2" in targets[1]["prompt"]
    assert targets[2]["turn_index"] == 3
    assert "Turn 3" in targets[2]["prompt"]

    # Test Mode 1: restore_conversation_only for Turn 3
    # Conversation is truncated back to Turn 2, but code remains at v2
    turn_3_msg_id = targets[2]["message_id"]
    mgr.undo(session_id, target_message_id=turn_3_msg_id, mode="restore_conversation_only")
    assert test_file.read_text() == "line 1\nline 2 v1\nline 3 v2\n"  # Code untouched
    msgs_after_conv = mgr.list_session_messages(session_id)
    user_msgs = [m for m in msgs_after_conv if m.role == "user"]
    assert len(user_msgs) == 2  # Only Turn 1 and Turn 2 remain

    # Test Mode 2: restore_code_only
    # Code reverts to checkpoint of Turn 2 (original lines), conversation remains 2 turns
    turn_2_msg_id = targets[1]["message_id"]
    mgr.undo(session_id, target_message_id=turn_2_msg_id, mode="restore_code_only")
    assert test_file.read_text() == "line 1\nline 2\nline 3\n"  # Code reverted
    user_msgs2 = [m for m in mgr.list_session_messages(session_id) if m.role == "user"]
    assert len(user_msgs2) == 2  # Conversation messages preserved

    # Test Mode 3: restore_both to Turn 1
    turn_1_msg_id = targets[0]["message_id"]
    mgr.undo(session_id, target_message_id=turn_1_msg_id, mode="restore_both")
    user_msgs3 = [m for m in mgr.list_session_messages(session_id) if m.role == "user"]
    assert len(user_msgs3) == 0


def test_select_undo_interactive_menu():
    """Test select_undo_interactive user selections."""
    sample_targets = [
        {"index": 1, "prompt": "Turn 1", "message_id": "m1", "checkpoint_hash": "abc"},
        {"index": 2, "prompt": "Turn 2", "message_id": "m2", "checkpoint_hash": "def"},
    ]

    with patch("builtins.input", side_effect=["1", "1"]):
        target, mode = select_undo_interactive(None, sample_targets)
        assert target is not None
        assert target["message_id"] == "m1"
        assert mode == "restore_both"

    with patch("builtins.input", side_effect=["2", "2"]):
        target, mode = select_undo_interactive(None, sample_targets)
        assert target is not None
        assert target["message_id"] == "m2"
        assert mode == "restore_conversation_only"

    with patch("builtins.input", side_effect=["2", "3"]):
        target, mode = select_undo_interactive(None, sample_targets)
        assert target is not None
        assert target["message_id"] == "m2"
        assert mode == "restore_code_only"


# ============================================================================
# 5. MCP Reconnect, Prompts, Resources, and CLI
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_manager_reconnect_prompts_and_resources():
    """Test MCP manager reconnect lifecycle, prompt listing, and resource reading."""
    mcp_mgr = McpManager()

    mock_client = MagicMock(spec=McpClient)
    mock_client.server_name = "test_server"
    mock_client.is_connected.return_value = True
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.list_tools = AsyncMock(
        return_value=[{"name": "fetch_data", "description": "Fetches data"}]
    )
    mock_client.list_prompts = AsyncMock(
        return_value=[{"name": "code_review", "description": "Review code prompt", "arguments": []}]
    )
    mock_client.list_resources = AsyncMock(
        return_value=[
            {"name": "schema", "uri": "file:///schema.json", "description": "JSON Schema"}
        ]
    )
    mock_client.read_resource = AsyncMock(
        return_value={"contents": [{"uri": "file:///schema.json", "text": '{"type": "object"}'}]}
    )

    with patch("coderai.core.mcp.manager.McpClient", return_value=mock_client):
        await mcp_mgr.initialize({"test_server": {"command": "node", "args": ["server.js"]}})

        assert len(mcp_mgr.tools) == 1
        assert len(mcp_mgr.prompts) == 1
        assert len(mcp_mgr.resources) == 1
        assert mcp_mgr.prompts[0]["original_name"] == "code_review"
        assert mcp_mgr.resources[0]["original_name"] == "schema"

        # Test read_resource
        res = await mcp_mgr.read_resource("file:///schema.json")
        assert res["contents"][0]["text"] == '{"type": "object"}'

        # Test reconnect
        reconnected = await mcp_mgr.reconnect("test_server")
        assert reconnected is True
        assert mock_client.disconnect.called


def test_mcp_completer():
    """Test that CoderAICompleter autocompletes /mcp subcommands."""
    completer = CoderAICompleter("/tmp")
    assert completer.complete("/mcp r", 0) == "reconnect"
    assert completer.complete("/mcp p", 0) == "prompts"
    assert completer.complete("/mcp res", 0) == "resources"
