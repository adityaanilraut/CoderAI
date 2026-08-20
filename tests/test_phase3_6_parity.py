"""Phases 3–6: presets/sandbox/hooks, session log, continuable agents, remaining dsh-base surface."""

from __future__ import annotations

import json
import pathlib

import pytest

from coderai.cli.completer import AVAILABLE_SLASH_COMMANDS
from coderai.core.agents import AgentHandle, get_agent_registry
from coderai.core.goals import get_goal_store, handle_goal_tool
from coderai.core.hooks import run_pre_tool_use
from coderai.core.mcp.client import McpClient
from coderai.core.mcp.transport import StreamableHttpMcpTransport
from coderai.core.network.security import is_same_origin
from coderai.core.prompt import TOOL_DOCS, get_system_prompt, get_tools
from coderai.core.prompt_sections import TOOL_ORDER, assemble_sections, order_tools, PromptSection
from coderai.core.sandbox import (
    apply_preset,
    build_seatbelt_profile,
    parse_sandbox_mode,
    preset_permissions,
    sandbox_policy_prompt,
    wrap_sandbox_command,
)
from coderai.core.session import SessionManager, SessionMessage
from coderai.core.session_log import derive_messages, prune_tool_results
from coderai.core.settings import resolve_current_settings
from coderai.core.tools.plan_mode import handle_exit_plan_mode_tool
from coderai.core.tools.todo_write import handle_todo_write_tool, todos_to_plan
from coderai.core.tools.types import ToolExecutionContext


def test_slash_commands_include_permission_and_goal():
    cmds = [c for c, _ in AVAILABLE_SLASH_COMMANDS]
    assert "/permission" in cmds
    assert "/goal" in cmds


def test_permission_presets_map_ten_scopes():
    ro = preset_permissions("read-only")
    assert "write-in-cwd" in ro["deny"]
    assert "read-in-cwd" in ro["allow"]
    assert ro["sandbox"] == "read-only"
    ws = preset_permissions("workspace-write")
    assert "write-in-cwd" in ws["allow"]
    assert "write-out-cwd" in ws["deny"]
    danger = preset_permissions("danger-full-access")
    assert danger["defaultMode"] == "allowAll"
    assert parse_sandbox_mode("readonly") == "read-only"
    merged = apply_preset({"allow": ["network"]}, "read-only")
    assert "network" in merged["allow"]
    assert merged["preset"] == "read-only"


def test_settings_preset_from_env(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("CODERAI_PERMISSION_PRESET", "read-only")
    settings = resolve_current_settings(str(tmp_path))
    assert settings["permissions"]["preset"] == "read-only"
    assert "write-in-cwd" in settings["permissions"]["deny"]


def test_seatbelt_profile_and_wrap_danger_is_noop():
    profile = build_seatbelt_profile("read-only", "/tmp/ws")
    assert "(deny default)" in profile
    assert "(allow file-read*)" in profile
    argv, meta = wrap_sandbox_command(
        ["echo", "hi"], mode="danger-full-access", workspace_root="/tmp"
    )
    assert argv == ["echo", "hi"]
    assert meta["sandboxApplied"] is False
    text = sandbox_policy_prompt("workspace-write", "/repo")
    assert "workspace-write" in text
    assert "/repo" in text


def test_pre_tool_use_hook_can_deny(tmp_path: pathlib.Path):
    hooks_dir = tmp_path / ".coderai"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "PreToolUse": [
                    {
                        "matcher": "bash",
                        "hooks": [{"command": "printf '%s\\n' '{\"decision\":\"block\"}'"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ctx = ToolExecutionContext(session_id="s", project_root=str(tmp_path))
    assert run_pre_tool_use("bash", {"command": "ls"}, ctx) == "deny"
    assert run_pre_tool_use("read", {"file_path": "x"}, ctx) == "allow"


def test_derive_messages_hides_replaced_ids_without_mutating_rows():
    msgs = [
        SessionMessage(id="s", session_id="x", role="system", content="sys"),
        SessionMessage(id="u1", session_id="x", role="user", content="old"),
        SessionMessage(id="a1", session_id="x", role="assistant", content="old-ack"),
        SessionMessage(
            id="sum",
            session_id="x",
            role="system",
            content="There are earlier parts of the conversation. Here is a summary:\n\nok",
            meta={"isSummary": True, "kind": "compact/summary", "replacedIds": ["u1", "a1"]},
        ),
        SessionMessage(id="u2", session_id="x", role="user", content="new"),
    ]
    derived = derive_messages(msgs)
    ids = [m.id for m in derived]
    assert "u1" not in ids
    assert "a1" not in ids
    assert "sum" in ids
    assert "u2" in ids
    assert msgs[1].content == "old"


def test_prune_tool_results_keeps_pairing_and_truncates():
    huge = "x" * 80_000
    tool = SessionMessage(id="t", session_id="x", role="tool", content=huge, tool_call_id="c1")
    pruned = prune_tool_results([tool], max_chars=100)
    assert len(pruned[0].content) < len(huge)
    assert "omitted" in pruned[0].content


@pytest.mark.asyncio
async def test_compact_appends_summary_event(tmp_path: pathlib.Path):
    class Msg:
        content = "Here is the summary."
        tool_calls = None
        reasoning_content = None
        refusal = None

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    class Completions:
        def create(self, **kwargs):
            return Resp()

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
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {"defaultMode": "allowAll"},
        },
    )
    sid = await mgr.create_session("start")
    for i in range(6):
        mgr._append_message(mgr._build_message(sid, "user", f"turn {i}"))
        mgr._append_message(mgr._build_assistant(sid, f"ack {i}", None))
    before = mgr.list_session_messages(sid)
    await mgr._compact_session(sid)
    after = mgr.list_session_messages(sid)
    assert len(after) == len(before) + 1
    assert any((m.meta or {}).get("kind") == "compact/summary" for m in after)
    assert not any(m.compacted for m in after if not (m.meta or {}).get("isSummary"))


def test_continuable_agent_registry_send_and_interrupt():
    reg = get_agent_registry()
    handle = AgentHandle(
        id="agent_test1", parent_session_id="parent", description="demo", mode="read_only"
    )
    reg.register(handle)
    sent = reg.send("agent_test1", "hello")
    assert sent is not None
    assert sent.inbox == ["hello"]
    interrupted = reg.interrupt("agent_test1")
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    listed = reg.list("parent")
    assert any(a.id == "agent_test1" for a in listed)


def test_todo_write_and_exit_plan_mode():
    plan = todos_to_plan(
        [
            {"id": "1", "content": "Do it", "status": "completed"},
            {"content": "Next", "status": "pending"},
        ]
    )
    assert "- [x] 1: Do it" in plan
    result = handle_todo_write_tool(
        {"todos": [{"content": "Ship", "status": "in_progress"}]},
        ToolExecutionContext(session_id="s", project_root="."),
    )
    assert result.ok
    assert result.metadata and result.metadata.get("todos")
    exit_res = handle_exit_plan_mode_tool({"summary": "Approved."}, None)
    assert exit_res.metadata and exit_res.metadata.get("exitPlanMode") is True


def test_goals_store_and_tool(tmp_path: pathlib.Path):
    store = get_goal_store(str(tmp_path))
    goal = store.add("sess", "Ship glob/grep")
    assert goal.id
    store.update("sess", goal.id, status="done")
    text = store.format("sess")
    assert "done" in text
    ctx = ToolExecutionContext(session_id="sess", project_root=str(tmp_path))
    listed = handle_goal_tool({"action": "list"}, ctx)
    assert listed.ok
    assert "Ship glob/grep" in (listed.output or "")


def test_get_tools_includes_phase6_and_stable_order():
    names = [t["function"]["name"] for t in get_tools()]
    assert "glob" in names
    assert "subagent" in names
    assert "todo_write" in names
    assert "exit_plan_mode" in names
    assert "goal" in names
    assert "report" not in names
    child = [t["function"]["name"] for t in get_tools({"childAgent": True})]
    assert "report" in child
    assert names.index("glob") < names.index("read")
    assert "subagent" in TOOL_ORDER
    ordered = order_tools([{"function": {"name": "WebSearch"}}, {"function": {"name": "bash"}}])
    assert ordered[0]["function"]["name"] == "bash"


def test_prompt_sections_and_sandbox_policy_section():
    text = assemble_sections([PromptSection("b", 20, "B"), PromptSection("a", 10, "A")])
    assert text.startswith("A")
    prompt = get_system_prompt({"sandboxMode": "read-only"})
    assert "read-only" in prompt
    assert "You are a helpful software engineer assistant." in prompt
    assert "## glob" in TOOL_DOCS


def test_same_origin_helper():
    assert is_same_origin("https://ex.com/a", "https://ex.com/b")
    assert not is_same_origin("https://ex.com/a", "https://other.com/a")
    assert not is_same_origin("https://ex.com/a", "http://ex.com/a")


def test_mcp_streamable_http_transport_selection():
    client = McpClient("demo", {"url": "https://example.com/mcp", "transport": "streamable-http"})
    assert isinstance(client.transport, StreamableHttpMcpTransport)
    sse = McpClient("demo2", {"url": "https://example.com/sse"})
    from coderai.core.mcp.transport import SseMcpTransport

    assert isinstance(sse.transport, SseMcpTransport)
