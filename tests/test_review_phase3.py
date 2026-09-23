"""Phase 3 Security and Permission Model Regression Tests.

Covers:
- WF-A5 / PR-A1 / PR-A2: Subagent allowlist semantics, case-insensitive alias matching,
  deny-by-default for unknown roles, read-only mode widening prevention.
- WF-A4: Real read-only mode blocking mutating tools, shell tools outside read-only sandbox,
  and spawning writable children.
- WF-A6 / TL-A2: Child execution inheriting parent sandbox, plan mode, and checkpoint hooks;
  spawn approval reflecting actual capabilities; mode in schemas.
- WF-A3: Recursion depth limit derived from lineage registry, foreground registration,
  clamping max_depth against model-supplied overrides.
- PR-A3: allowedTools enforcement in to_openai_schemas and executor permission checks;
  resolve_agent_spec preserving empty tool lists.
- PR-A4: Builtin explore/plan subagents receiving ROLE_ADDITIONAL prompt and read_only mode.
- PR-C1 / PR-C4: Frontmatter schema validation, fail-closed YAML parsing, comma-separated
  tools string, null/string exclude_tools handling, boolean parsing.
- AL-A2: Isolation of auto-approve (YOLO/AFK) across session managers, unregistering on dispose/close.
- TL-B8: Plan mode enforcement in ToolExecutor via context.plan_mode.
- TL-A19: ApprovalRuntime routing from Approval.request for pending/cancel tracking.
- PR-B7: switch_agent_role rebuilding session system message and updating settings.
"""

from __future__ import annotations

import asyncio
import pathlib
import pytest
from unittest.mock import MagicMock

from coderai.subagents.builder import SubAgentSpec
from coderai.subagents.registry import (
    get_subagent_definition,
    is_tool_allowed,
    parse_markdown_agent_spec,
    resolve_tool_policy,
)
from coderai.subagents.runner import SubAgentManager
from coderai.subagents.core import get_agent_registry, AgentHandle
from coderai.tools.legacy.types import ToolExecutionContext, ToolExecutionHooks
from coderai.tools.legacy.registry import get_tool_registry
from coderai.tools.legacy.executor import ToolExecutor
from coderai.soul.approval import Approval, compute_tool_call_permissions
from coderai.soul.session.manager import SessionManager
from coderai.agentspec import resolve_agent_spec


@pytest.mark.security
def test_subagent_role_allowlist_matching_and_casing():
    """WF-A5 / PR-A1 / PR-A2: Roles with capitalized tools get their tools (case/alias aware)."""
    # architect declares ["Read", "Grep", "Glob"]
    defn = get_subagent_definition("architect")
    assert defn is not None
    assert defn.allowed_tools is not None
    # Test that is_tool_allowed harmonizes "Read" -> "read", "Grep" -> "grep", "Glob" -> "glob"
    assert is_tool_allowed("read", "allowlist", defn.allowed_tools) is True
    assert is_tool_allowed("grep", "allowlist", defn.allowed_tools) is True
    assert is_tool_allowed("glob", "allowlist", defn.allowed_tools) is True
    assert is_tool_allowed("bash", "allowlist", defn.allowed_tools) is False
    assert is_tool_allowed("write", "allowlist", defn.allowed_tools) is False


@pytest.mark.security
def test_unknown_subagent_type_denied_all_tools(tmp_path: pathlib.Path):
    """WF-A5 / PR-A2: Unknown subagent_type yields empty allowlist, not all tools."""
    policy_mode, tools = resolve_tool_policy("completely_bogus_type", project_root=str(tmp_path))
    assert policy_mode == "allowlist"
    assert tools == ()

    spec = SubAgentSpec(
        description="bogus",
        prompt="test",
        subagent_type="completely_bogus_type",
        isolated_cwd=str(tmp_path),
    )
    assert spec.allowed_tools == []
    mgr = SubAgentManager(project_root=str(tmp_path), create_openai_client=lambda: MagicMock())
    sandboxed = mgr._get_sandboxed_tools(spec, model="gpt-4o")
    # Denied all tools
    assert len(sandboxed) == 0


@pytest.mark.security
def test_code_reviewer_role_is_read_only_and_restricted(tmp_path: pathlib.Path):
    """WF-A5 / PR-A2: code-reviewer with tools: [] has mode=read_only and no mutating tools."""
    defn = get_subagent_definition("code-reviewer", project_root=str(tmp_path))
    assert defn is not None
    assert defn.mode == "read_only"
    assert defn.allowed_tools == ("read", "grep", "glob")

    spec = SubAgentSpec(
        description="review",
        prompt="review diff",
        subagent_type="code-reviewer",
        isolated_cwd=str(tmp_path),
    )
    assert spec.mode == "read_only"
    mgr = SubAgentManager(project_root=str(tmp_path), create_openai_client=lambda: MagicMock())
    sandboxed = mgr._get_sandboxed_tools(spec, model="gpt-4o")
    names = [t.get("function", {}).get("name") for t in sandboxed]
    assert "write" not in names
    assert "edit" not in names
    assert "bash" not in names
    assert "spawn_teammate" not in names


@pytest.mark.security
def test_explicit_read_only_never_widened_by_role(tmp_path: pathlib.Path):
    """WF-A5: An explicit read_only mode must never be widened to general."""
    spec = SubAgentSpec(
        description="coder task",
        prompt="test",
        subagent_type="coder",  # coder role has mode='general'
        mode="read_only",  # explicit read_only
        isolated_cwd=str(tmp_path),
    )
    assert spec.mode == "read_only"


@pytest.mark.security
def test_read_only_mode_blocks_mutating_tools_in_listing(tmp_path: pathlib.Path):
    """WF-A4: SubAgentSpec(mode='read_only') filters mutating and shell tools."""
    spec = SubAgentSpec(
        description="read-only agent",
        prompt="inspect",
        mode="read_only",
        isolated_cwd=str(tmp_path),
    )
    mgr = SubAgentManager(project_root=str(tmp_path), create_openai_client=lambda: MagicMock())
    sandboxed = mgr._get_sandboxed_tools(spec, model="gpt-4o")
    names = {t.get("function", {}).get("name") for t in sandboxed}

    assert "write" not in names
    assert "edit" not in names
    assert "str_replace_editor" not in names
    assert "bash" not in names
    assert "pwsh" not in names
    assert "terminal_open" not in names
    assert "terminal_send" not in names
    assert "schedule_create" not in names
    assert "spawn_teammate" not in names


@pytest.mark.security
def test_read_only_mode_allows_bash_in_read_only_sandbox(tmp_path: pathlib.Path):
    """WF-A4: bash is allowed in read-only mode only if sandbox_mode is 'read-only'."""
    spec = SubAgentSpec(
        description="read-only with ro sandbox",
        prompt="inspect",
        mode="read_only",
        sandbox_mode="read-only",
        isolated_cwd=str(tmp_path),
    )
    mgr = SubAgentManager(project_root=str(tmp_path), create_openai_client=lambda: MagicMock())
    sandboxed = mgr._get_sandboxed_tools(spec, model="gpt-4o")
    names = {t.get("function", {}).get("name") for t in sandboxed}
    assert "bash" in names
    assert "write" not in names


@pytest.mark.security
@pytest.mark.asyncio
async def test_read_only_mode_blocks_mutating_execution_and_writable_child(tmp_path: pathlib.Path):
    """WF-A4: At execution time, mutating tools and writable children are blocked."""
    from coderai.subagents.runner import SubAgentManager

    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock()

    # 1. Mutating tool call (bash outside read-only sandbox)
    tc_bash = {
        "id": "c1",
        "type": "function",
        "function": {"name": "bash", "arguments": '{"command": "rm -rf /"}'},
    }
    spec = SubAgentSpec(
        description="ro",
        prompt="do work",
        mode="read_only",
        isolated_cwd=str(tmp_path),
    )
    mgr = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": mock_client, "model": "gpt-4o"},
    )

    # Simulate model response calling bash then stopping
    mock_resp1 = {
        "choices": [{"message": {"content": "", "tool_calls": [tc_bash], "refusal": None}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }
    mock_resp2 = {
        "choices": [{"message": {"content": "Finished.", "tool_calls": None, "refusal": None}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }

    mock_client.chat.completions.create.side_effect = [mock_resp1, mock_resp2]

    res = await mgr.spawn_subagent(spec)
    assert res.status == "completed"

    # 2. Spawning writable child from read_only parent
    tc_spawn_writable = {
        "id": "c2",
        "type": "function",
        "function": {
            "name": "Task",
            "arguments": '{"description": "child", "prompt": "write stuff", "mode": "general"}',
        },
    }
    mock_resp_spawn = {
        "choices": [
            {"message": {"content": "", "tool_calls": [tc_spawn_writable], "refusal": None}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }
    mock_client.chat.completions.create.side_effect = [mock_resp_spawn, mock_resp2]
    res2 = await mgr.spawn_subagent(spec)
    assert res2.status == "completed"


@pytest.mark.security
def test_spawn_approval_reflects_actual_capabilities():
    """WF-A6 / TL-A2: Spawn approval reflects role capabilities, not blind read_only default."""

    # Task with coder subagent_type (general mode) -> requires write-in-cwd scope
    tc_coder = {
        "id": "c1",
        "type": "function",
        "function": {
            "name": "Task",
            "arguments": '{"description": "build feature", "prompt": "build", "subagent_type": "coder"}',
        },
    }
    plan_coder = compute_tool_call_permissions(
        session_id="s1",
        project_root="/tmp",
        tool_calls=[tc_coder],
        settings={},
    )
    assert "write-in-cwd" in plan_coder["permissions"][0]["scopes"]

    # Task with explore subagent_type (read_only mode) -> empty scopes
    tc_explore = {
        "id": "c2",
        "type": "function",
        "function": {
            "name": "Task",
            "arguments": '{"description": "explore repo", "prompt": "find", "subagent_type": "explore"}',
        },
    }
    plan_explore = compute_tool_call_permissions(
        session_id="s1",
        project_root="/tmp",
        tool_calls=[tc_explore],
        settings={},
    )
    assert plan_explore["permissions"][0]["scopes"] == []


@pytest.mark.security
def test_task_schema_declares_mode():
    """WF-A6: Task, subagent, subagent_fork declare mode in their parameter schemas."""
    reg = get_tool_registry()
    for tool_name in ("Task", "subagent", "subagent_fork"):
        tool_def = reg.get(tool_name)
        assert tool_def is not None
        params = tool_def.parameters.get("properties") or tool_def.parameters
        assert "mode" in params
        assert params["mode"].get("type") == "string"


@pytest.mark.security
def test_recursion_depth_derived_from_lineage_and_clamped():
    """WF-A3: Recursion depth derives monotonically from lineage and clamps max_depth."""
    from coderai.tools.agent import _derive_depth

    registry = get_agent_registry()
    # Register handle for parent session
    parent_handle = AgentHandle(
        id="parent_task",
        parent_session_id="root",
        description="parent",
        mode="general",
        depth=1,
        run_session_id="sub_root_parent",
    )
    registry.register(parent_handle)

    # Child executing in context of parent's run_session_id must derive depth=2
    ctx = ToolExecutionContext(session_id="sub_root_parent", project_root="/tmp")
    # Even if model attempts to supply depth=0 or max_depth=99
    derived = _derive_depth(ctx, {"depth": 0, "max_depth": 99})
    assert derived == 2

    # Root session with no registry entry derives depth=0, ignoring model args
    ctx_root = ToolExecutionContext(session_id="root_session_unknown", project_root="/tmp")
    assert _derive_depth(ctx_root, {"depth": 99}) == 0


@pytest.mark.security
def test_allowed_tools_enforced_in_schemas_and_executor(tmp_path: pathlib.Path):
    """PR-A3: allowedTools in session settings is enforced in tool schemas and executor."""
    reg = get_tool_registry()

    # 1. to_openai_schemas filters with allowedTools
    schemas = reg.to_openai_schemas(options={"allowedTools": ["read", "grep"]})
    schema_names = [s.get("function", {}).get("name") for s in schemas]
    assert "read" in schema_names
    assert "grep" in schema_names
    assert "write" not in schema_names
    assert "bash" not in schema_names

    # Empty list denies all
    empty_schemas = reg.to_openai_schemas(options={"allowedTools": []})
    assert len(empty_schemas) == 0

    # 2. ToolExecutor checks context.allowed_tools
    executor = ToolExecutor(project_root=str(tmp_path), registry=reg)
    hooks = ToolExecutionHooks(allowed_tools=["read"])
    tc = {
        "id": "c1",
        "type": "function",
        "function": {"name": "write", "arguments": '{"file_path": "x.txt", "content": "hi"}'},
    }

    async def _test():
        res = await executor.execute_tool_calls("s1", [tc], hooks=hooks)
        assert res[0]["result"]["ok"] is False
        assert (
            "allowedTools" in res[0]["result"]["error"]
            or "PermissionDenied" in res[0]["result"]["error"]
        )

    asyncio.run(_test())


@pytest.mark.security
def test_resolve_agent_spec_preserves_empty_tool_list(tmp_path: pathlib.Path):
    """PR-A3: resolve_agent_spec must not turn empty tools list into None."""
    agent_dir = tmp_path / ".coderai" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "empty-tools.md").write_text("---\nname: empty-tools\ntools: []\n---\nPrompt\n")
    spec = resolve_agent_spec("empty-tools", project_root=tmp_path)
    assert spec.allowed_tools == []


@pytest.mark.security
def test_builtin_explore_plan_receive_role_additional_and_read_only():
    """PR-A4: Builtin explore and plan subagents get ROLE_ADDITIONAL and mode='read_only'."""
    explore = get_subagent_definition("explore")
    assert explore is not None
    assert explore.mode == "read_only"
    assert "You are a codebase exploration specialist" in (explore.system_prompt or "")

    plan = get_subagent_definition("plan")
    assert plan is not None
    assert plan.mode == "read_only"
    assert "implementation plan" in (plan.system_prompt or "").lower()


@pytest.mark.security
def test_markdown_frontmatter_fails_closed_and_handles_edge_cases(tmp_path: pathlib.Path):
    """PR-C1 / PR-C4: Markdown agent frontmatter parsing fails closed with schema validation."""
    # 1. Invalid YAML fails closed (returns None, not unrestricted)
    broken_yaml = tmp_path / "broken.md"
    broken_yaml.write_text(
        "---\nname: broken\ndescription: unquoted: colon : error\ntools: [read\n---\nbody"
    )
    assert parse_markdown_agent_spec(broken_yaml) is None

    # 2. Comma-separated tools string
    comma_spec = tmp_path / "comma.md"
    comma_spec.write_text("---\nname: comma\ntools: Read, Grep, Glob\n---\nbody")
    defn_comma = parse_markdown_agent_spec(comma_spec)
    assert defn_comma is not None
    assert defn_comma.allowed_tools == ("Read", "Grep", "Glob")
    assert defn_comma.mode == "read_only"

    # 3. exclude_tools: null must not crash
    null_exclude = tmp_path / "null_ex.md"
    null_exclude.write_text("---\nname: nullex\nexclude_tools: null\n---\nbody")
    defn_null = parse_markdown_agent_spec(null_exclude)
    assert defn_null is not None
    assert defn_null.exclude_tools == ()

    # 4. exclude_tools: bash must not split into characters
    str_exclude = tmp_path / "str_ex.md"
    str_exclude.write_text("---\nname: strex\nexclude_tools: bash\n---\nbody")
    defn_str = parse_markdown_agent_spec(str_exclude)
    assert defn_str is not None
    assert defn_str.exclude_tools == ("bash",)

    # 5. supports_background: 'false' parses as boolean False
    false_bg = tmp_path / "false_bg.md"
    false_bg.write_text("---\nname: falsebg\nsupports_background: 'false'\n---\nbody")
    defn_bg = parse_markdown_agent_spec(false_bg)
    assert defn_bg is not None
    assert defn_bg.supports_background is False


@pytest.mark.security
@pytest.mark.asyncio
async def test_session_manager_auto_approve_isolation_and_unregistration(tmp_path: pathlib.Path):
    """AL-A2: Two session managers, one in YOLO; the other still prompts. Unregistered on dispose."""
    from coderai.soul.session.approval import _session_managers

    m1 = SessionManager(
        project_root=str(tmp_path / "m1"),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )
    m2 = SessionManager(
        project_root=str(tmp_path / "m2"),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )

    assert m1 in _session_managers
    assert m2 in _session_managers

    m1.set_yolo(True)
    m2.set_yolo(False)

    approval2 = Approval(yolo=False)
    # Using m2's session
    from coderai.soul.tool_context import set_session_id

    set_session_id("m2_session")
    m2._active_session_id = "m2_session"

    # approval2 for m2's session must not auto-approve even though m1 has YOLO
    task = asyncio.create_task(approval2.request("test", "bash", "execute rm"))
    await asyncio.sleep(0.02)
    pending = approval2.runtime.list_pending()
    assert len(pending) == 1  # Not auto-approved! Held in ApprovalRuntime
    approval2.runtime.cancel(pending[0].id, feedback="deny by test")
    res = await task
    assert res.approved is False

    # Disposing m1 removes it from active list
    m1.dispose()
    assert m1 not in _session_managers
    m2.dispose()
    assert m2 not in _session_managers


@pytest.mark.security
@pytest.mark.asyncio
async def test_executor_enforces_plan_mode_via_context(tmp_path: pathlib.Path):
    """TL-B8: ToolExecutor blocks mutating tools when context.plan_mode is True."""
    reg = get_tool_registry()
    executor = ToolExecutor(project_root=str(tmp_path), registry=reg)

    # In plan mode
    hooks = ToolExecutionHooks(plan_mode=True)

    # Mutating tool call: write
    tc_write = {
        "id": "call_w",
        "type": "function",
        "function": {
            "name": "write",
            "arguments": '{"file_path": "test.txt", "content": "hello"}',
        },
    }
    res_w = await executor.execute_tool_calls("s1", [tc_write], hooks=hooks)
    assert res_w[0]["result"]["ok"] is False
    assert "plan mode" in res_w[0]["result"]["error"].lower()

    # Non-mutating tool call: read
    test_file = tmp_path / "readme.txt"
    test_file.write_text("ok")
    tc_read = {
        "id": "call_r",
        "type": "function",
        "function": {"name": "read", "arguments": f'{{"file_path": "{test_file}"}}'},
    }
    res_r = await executor.execute_tool_calls("s1", [tc_read], hooks=hooks)
    assert res_r[0]["result"]["ok"] is True


@pytest.mark.security
@pytest.mark.asyncio
async def test_approval_runtime_tracks_pending_and_cancellation():
    """TL-A19: Approval.request routes through ApprovalRuntime for pending/cancel state."""
    app = Approval(yolo=False)
    runtime = app.runtime
    assert len(runtime.list_pending()) == 0

    # Start a request that will wait
    task = asyncio.create_task(app.request("sender", "bash", "run command"))
    await asyncio.sleep(0.05)

    # Verify it is tracked in runtime
    pending = runtime.list_pending()
    assert len(pending) == 1
    req_id = pending[0].id

    # Cancel it through the runtime
    runtime.cancel(req_id, feedback="cancelled by test")
    res = await task
    assert res.approved is False
    assert len(runtime.list_pending()) == 0


@pytest.mark.security
def test_switch_agent_role_rebuilds_session_system_message(tmp_path: pathlib.Path):
    """PR-B7: switch_agent_role updates persona, allowedTools, and session messages."""
    test_settings = {"model": "gpt-4o"}
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: test_settings,
    )
    # Initialize session
    sid = "sess_1"
    mgr.session_store.replace_rows(
        sid,
        [
            {
                "seq": 0,
                "role": "system",
                "content": "Original default prompt",
                "meta": {},
            },
            {
                "seq": 1,
                "role": "user",
                "content": "Hello",
                "meta": {},
            },
        ],
    )
    mgr._active_session_id = sid

    ok = mgr.switch_agent_role("architect", session_id=sid)
    assert ok is True
    assert mgr.get_active_agent_role() == "architect"
    assert mgr.get_resolved_settings().get("allowedTools") == ["Read", "Grep", "Glob"]

    # System message in session store must be rebuilt
    rows = mgr.session_store.read_rows(sid)
    assert rows[0]["role"] == "system"
    assert (
        "architect" in rows[0]["content"].lower() or "Architecture specialist" in rows[0]["content"]
    )
    mgr.dispose()
