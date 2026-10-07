"""Exercise teammate tasks through the real spec builder and child runner."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from coderai.sandbox import preset_permissions
from coderai.subagents.builder import SubAgentSpec, build_spec
from coderai.subagents.core import AgentHandle, AgentRegistry, get_agent_registry
from coderai.teams.manager import TeamManager
from coderai.teams.tools import handle_spawn_teammate_tool
from coderai.tools.legacy.types import ToolExecutionContext


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr("coderai.subagents.core._registry", AgentRegistry())
    team = TeamManager()

    async def no_worker(teammate_id):
        pass

    monkeypatch.setattr(team, "_teammate_worker", no_worker)
    monkeypatch.setattr("coderai.teams.tools.get_team_manager", lambda: team)
    requests = []
    responses = []
    roots = []
    client = object()
    factories = []
    settings = {"permissions": preset_permissions("workspace-write")}
    callbacks = []

    def create_client():
        factories.append(True)
        return {"client": client, "model": "parent-model", "baseURL": "https://parent.invalid"}

    def completion(actual_client, request):
        assert actual_client is client
        requests.append(copy.deepcopy(request))
        message = responses.pop(0) if responses else {"content": "Task completed."}
        return {"choices": [{"message": message}]}

    def runtime_context(actual_root, model):
        roots.append(actual_root)
        return ""

    monkeypatch.setattr("coderai.subagents.runner._call_llm_sync", completion)
    monkeypatch.setattr("coderai.subagents.runner.get_runtime_context", runtime_context)
    context = ToolExecutionContext(
        session_id="original-parent",
        project_root=str(root),
        create_openai_client=create_client,
        session_manager=SimpleNamespace(get_resolved_settings=lambda: settings),
        sandbox_mode="workspace-write",
        allowed_tools=["read", "write", "spawn_teammate"],
        on_before_file_mutation=lambda fp: callbacks.append(("before", fp)),
        on_after_file_mutation=lambda fp: callbacks.append(("after", fp)),
    )
    return SimpleNamespace(
        root=root,
        team=team,
        context=context,
        requests=requests,
        responses=responses,
        roots=roots,
        factories=factories,
        settings=settings,
        callbacks=callbacks,
    )


async def spawn(runtime, **args):
    result = await handle_spawn_teammate_tool(
        {"name": "teammate", "role": "coder", **args}, runtime.context
    )
    assert result.ok, result.error
    teammate = runtime.team.get_teammate(result.metadata["teammate_id"])
    task = runtime.team.task_board.create_task("Review code", "Inspect the original project.")
    return teammate, task


def tool_call(name, args):
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def child(runtime):
    handles = get_agent_registry().list(parent_session_id=runtime.context.session_id)
    assert len(handles) == 1
    return handles[0]


async def test_original_workspace_provider_and_runtime_reach_real_child(runtime):
    runtime.settings["orchestration"] = {"maxDepth": 2, "maxIterations": 4, "timeoutSeconds": 7}
    teammate, task = await spawn(runtime, allowed_tools=["read", "write", "bash"])
    assert await runtime.team._execute_task(teammate, task) == "Task completed."

    handle = child(runtime)
    assert teammate.project_root == str(runtime.root)
    assert handle.parent_session_id == "original-parent"
    assert handle.depth == 0
    assert handle.spec.project_root == str(runtime.root)
    assert handle.spec.max_depth == 2
    assert handle.spec.max_iterations == 4
    assert handle.spec.timeout_seconds == 7
    assert handle.spec.sandbox_mode == "workspace-write"
    assert handle.spec.allowed_tools == ["read", "write"]
    assert handle.spec.session_manager is runtime.context.session_manager
    assert runtime.roots == [str(runtime.root)]
    assert runtime.factories == [True]
    assert runtime.requests[0]["model"] == "parent-model"
    names = {t["function"]["name"] for t in runtime.requests[0]["tools"]}
    assert names == {"read", "write"}


async def test_parent_mutation_callbacks_follow_real_child_write(runtime):
    target = runtime.root / "created.py"
    runtime.responses.append(
        {"tool_calls": [tool_call("write", {"file_path": str(target), "content": "value = 1\n"})]}
    )
    teammate, task = await spawn(runtime)
    await runtime.team._execute_task(teammate, task)
    assert target.read_text() == "value = 1\n"
    assert runtime.callbacks == [("before", str(target)), ("after", str(target))]


async def test_isolated_parent_keeps_original_owner_root_and_dry_run(runtime):
    isolation = runtime.root / "isolated"
    isolation.mkdir()
    runtime.context.session_manager.project_root = str(runtime.root)
    runtime.context.project_root = str(isolation)
    runtime.context.isolated_cwd = str(isolation)
    runtime.context.dry_run = True
    target = isolation / "preview.py"
    runtime.responses.append(
        {"tool_calls": [tool_call("write", {"file_path": str(target), "content": "preview\n"})]}
    )
    teammate, task = await spawn(runtime)
    await runtime.team._execute_task(teammate, task)
    spec = child(runtime).spec
    assert teammate.project_root == spec.project_root == str(runtime.root)
    assert spec.isolated_cwd == str(isolation)
    assert spec.dry_run
    assert runtime.roots == [str(isolation)]
    assert not target.exists()
    assert runtime.callbacks == []


@pytest.mark.parametrize(
    "constraint", ["plan", "read-only", "empty-allowlist", "scope-deny", "scope-ask"]
)
async def test_parent_constraints_block_real_child_mutation(runtime, constraint):
    target = runtime.root / "forbidden.py"
    if constraint == "plan":
        runtime.context.plan_mode = True
    elif constraint == "read-only":
        runtime.context.sandbox_mode = "read-only"
    elif constraint == "empty-allowlist":
        runtime.context.allowed_tools = []
    elif constraint == "scope-deny":
        runtime.settings["permissions"]["deny"].append("write-in-cwd")
    else:
        runtime.settings["permissions"]["ask"].append("write-in-cwd")
    runtime.responses.append(
        {"tool_calls": [tool_call("write", {"file_path": str(target), "content": "forbidden\n"})]}
    )
    teammate, task = await spawn(runtime, allowed_tools=["read", "write", "bash"])
    await runtime.team._execute_task(teammate, task)
    assert not target.exists()
    assert runtime.callbacks == []
    errors = [m["content"] for m in runtime.requests[-1]["messages"] if m["role"] == "tool"]
    assert errors and json.loads(errors[-1])["ok"] is False
    if constraint == "plan":
        assert child(runtime).spec.plan_mode
    if constraint == "empty-allowlist":
        assert child(runtime).spec.allowed_tools == []


@pytest.mark.parametrize("parent_settings", [{"permissions": {}}, {}])
async def test_empty_parent_permission_settings_keep_default_ask_policy(runtime, parent_settings):
    runtime.settings.clear()
    runtime.settings.update(parent_settings)
    target = runtime.root / "unapproved.py"
    runtime.responses.append(
        {"tool_calls": [tool_call("write", {"file_path": str(target), "content": "no\n"})]}
    )
    teammate, task = await spawn(runtime)
    await runtime.team._execute_task(teammate, task)
    assert not target.exists()
    errors = [m["content"] for m in runtime.requests[-1]["messages"] if m["role"] == "tool"]
    assert errors and "requires approval" in errors[-1]


async def test_workspace_role_is_resolved_from_original_project(runtime):
    agents = runtime.root / ".coderai" / "agents"
    agents.mkdir(parents=True)
    (agents / "project-reviewer.md").write_text(
        "---\nname: project-reviewer\ndescription: Review project\ntools: [read]\nmode: read_only\n---\nProject-specific reviewer instructions.\n"
    )
    teammate, task = await spawn(runtime, role="project-reviewer")
    await runtime.team._execute_task(teammate, task)
    spec = child(runtime).spec
    assert spec.mode == "read_only"
    assert spec.allowed_tools == ["read"]
    assert "Project-specific reviewer instructions" in spec.system_prompt


async def test_nested_teammate_inherits_lineage_and_enforces_parent_depth_cap(runtime):
    runtime.settings["orchestration"] = {"maxDepth": 10}
    parent = AgentHandle(
        id="nested-parent",
        parent_session_id="root-session",
        run_session_id=runtime.context.session_id,
        description="Parent task",
        mode="general",
        depth=1,
        root_agent_id="original-root",
        spec=SubAgentSpec(description="parent", prompt="work", depth=1, max_depth=3),
    )
    get_agent_registry().register(parent)
    assert (
        build_spec(runtime.context, description="child", prompt="work", max_depth=99).max_depth == 3
    )
    teammate, task = await spawn(runtime)
    await runtime.team._execute_task(teammate, task)
    handle = child(runtime)
    assert teammate.depth == handle.depth == 2
    assert handle.spec.max_depth == 3
    assert handle.parent_agent_id == parent.id
    assert handle.root_agent_id == "original-root"
    assert handle.id in parent.children_ids
    runtime.context.session_id = handle.run_session_id
    second, second_task = await spawn(runtime, name="grandchild")
    with pytest.raises(RuntimeError, match="nesting depth|RecursionLimitError"):
        await runtime.team._execute_task(second, second_task)
    assert len(runtime.requests) == 1


async def test_child_provider_output_does_not_leak_into_parent_emitter(runtime, monkeypatch):
    from coderai.wire.emitter import WireEmitter, bind_emitter, get_emitter, reset_emitter
    from coderai.wire.types import TextPart

    parent_emitter = WireEmitter()
    child_emitters = []

    def completion(actual_client, request):
        child_emitters.append(get_emitter())
        get_emitter().send(TextPart(text="private child output"))
        assert get_emitter().event_count(TextPart) == 1
        return {"choices": [{"message": {"content": "Completed privately."}}]}

    monkeypatch.setattr("coderai.subagents.runner._call_llm_sync", completion)
    token = bind_emitter(parent_emitter)
    try:
        teammate, task = await spawn(runtime)
        await runtime.team._execute_task(teammate, task)
        assert get_emitter() is parent_emitter
        assert child_emitters[0] is not parent_emitter
        assert child_emitters[0].event_count(TextPart) == 0
        assert child_emitters[0].buffered() == []
        assert parent_emitter.event_count(TextPart) == 0
    finally:
        parent_emitter.close()
        reset_emitter(token)


async def test_child_shell_callbacks_remain_bound_to_owning_parent(runtime, monkeypatch):
    from coderai.tools.legacy.registry import get_tool_registry
    from coderai.tools.legacy.types import ToolResult

    callbacks = []
    owner_sid = runtime.context.session_id
    runtime.context.allowed_tools = ["bash"]
    runtime.settings["permissions"] = preset_permissions("danger-full-access")
    runtime.context.on_process_start = lambda pid, cmd: callbacks.append(
        ("start", owner_sid, pid, cmd)
    )
    runtime.context.on_process_exit = lambda pid: callbacks.append(("exit", owner_sid, pid))
    runtime.context.on_process_stdout = lambda pid, text: callbacks.append(("stdout", pid, text))
    runtime.context.on_process_timeout_control = lambda pid, control: callbacks.append(
        ("timeout", pid, control)
    )
    runtime.context.on_background_process_complete = lambda completion: callbacks.append(
        ("background", completion)
    )
    child_sids = []

    def shell_handler(args, context):
        child_sids.append(context.session_id)
        context.on_process_start(456, args["command"])
        context.on_process_stdout(456, "child output")
        context.on_process_timeout_control(456, None)
        context.on_background_process_complete("completion")
        context.on_process_exit(456)
        return ToolResult(ok=True, name="bash", output="done")

    monkeypatch.setattr(get_tool_registry().get_tool("bash"), "handler", shell_handler)
    runtime.responses.append(
        {"tool_calls": [tool_call("bash", {"command": "pwd", "sideEffects": ["read-in-cwd"]})]}
    )
    teammate, task = await spawn(runtime, allowed_tools=["bash"])
    await runtime.team._execute_task(teammate, task)
    assert child_sids == [child(runtime).run_session_id], runtime.requests[-1]["messages"]
    assert child_sids[0] != owner_sid
    assert callbacks == [
        ("start", owner_sid, 456, "pwd"),
        ("stdout", 456, "child output"),
        ("timeout", 456, None),
        ("background", "completion"),
        ("exit", owner_sid, 456),
    ]
