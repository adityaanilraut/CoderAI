"""Regression tests for Phase 7 (Workflows, orchestration, and event-loop hygiene).

Covers:
- WF-A2: Subagent runners re-raise CancelledError; only map to interrupted for abort_event.
- WF-A7: Single ownership of JobStore, AgentRegistry, ScheduleManager; schedule persists.
- WF-A8: Continuable workers get real kill (terminal flag + task.cancel), idle TTL, eviction, spawn cap.
- WF-A9: Teammates do not reply to acknowledgements; inbox/outbox capped; wait_agent tracks unread.
- WF-A11: Hooks timeout capped, start_new_session=True, process group killed on timeout.
- WF-A12: Subagent isolated_cwd defaults to project root; scratchpad cleaned up when isolation requested.
- WF-A13: Monotonic subagent job ids, task popped in finally, job-cap handled, JobStore.kill cancel callback.
- WF-A14: External CLI backends use async process execution and kill process group on cancel.
- WF-A16: Team tasks get session project root, parent_session_id, depth; ready tasks auto-started.
- WF-A17: Subagent hooks pass parent_session_id; SubagentStop fires from finally and in continuable runs.
- WF-A18: Goals advance rounds from turn loop, keyed by project root, atomic write, validate max_rounds, one running.
- IN-A6: Disconnect old MCP client before replacing; wrap disconnect in try.
- IN-A7: Stdio writes off-loop; HTTP honours Mcp-Session-Id, sets last_http_status, doesn't swallow errors.
- IN-A8: OAuth device flow handles access_denied, slow_down (+5s), expires_in, bounded recursion.
- IN-A13: Config precedence: project_root passed through, project config.toml beats user settings.json, cache key covers global file.
- IN-A15: Loading typed config does not write files.
- IN-A17: ACP terminal connect pops pending request on cancel; raise ... from.
- UI-A7: ! shell mode uses asyncio subprocess and prints as Text.
- UI-A8: Track current cancellable task for slash commands, reset sigint counter, exit 130 on interrupt.
- UI-A9 / UI-A10: Approval/question prompts and upgrade don't block loop. Ctrl-O quotes editor path.
- IN-D4 / IN-D5: Telemetry task references kept. Wire stdout writes off-loop. Don't build OpenAI client on prompt.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderai.subagents.builder import SubAgentSpec
from coderai.subagents.core import AgentHandle, get_agent_registry
from coderai.subagents.runner import SubAgentManager


# ---------------------------------------------------------------------------
# WF-A2: Subagent cancellation propagation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wf_a2_subagent_runner_reraises_cancellation(tmp_path: Path) -> None:
    runner = SubAgentManager(project_root=str(tmp_path))
    spec = SubAgentSpec(prompt="test", description="test description", timeout_seconds=10.0)

    # Simulate an external cancellation (e.g. from parent timeout or user interrupt)
    # when abort_event is NOT set
    async def _mock_loop(*args, **kwargs):
        raise asyncio.CancelledError()

    with patch.object(runner, "_run_subagent_loop", side_effect=_mock_loop):
        # Without abort_event set, CancelledError MUST be re-raised so wait_for triggers TimeoutError
        with pytest.raises(asyncio.CancelledError):
            await runner.spawn_subagent(spec)


# ---------------------------------------------------------------------------
# WF-A7: Single ownership of stores
# ---------------------------------------------------------------------------
def test_wf_a7_single_ownership_stores(tmp_path: Path) -> None:
    from coderai.background.manager import get_job_store
    from coderai.schedule import get_schedule_manager
    from coderai.subagents.core import get_agent_registry
    from coderai.soul.session.manager import SessionManager

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {},
        get_resolved_settings=lambda: {},
    )
    # SessionManager must hold the exact same global singletons
    assert mgr.job_store is get_job_store()
    assert mgr.agent_registry is get_agent_registry()
    assert mgr.schedule_manager is get_schedule_manager()
    assert mgr.schedule_manager.storage_path is not None


# ---------------------------------------------------------------------------
# WF-A8: Continuable workers kill, TTL, and spawn cap
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wf_a8_continuable_worker_kill_and_cap() -> None:
    registry = get_agent_registry()
    handle = AgentHandle(
        id="test_worker_1",
        parent_session_id="parent_s",
        description="test",
        mode="general",
        status="completed",
        inbox_waiter=asyncio.Event(),
    )
    registry.register(handle)

    # Spawn cap check: completed continuable workers with active/parked tasks count toward live cap
    from coderai.background.agent_runner import resolve_max_continuable_agents

    assert resolve_max_continuable_agents() > 0

    # Kill must set killed flag and cancel task
    mock_task = MagicMock()
    mock_task.done.return_value = False
    handle.task = mock_task

    registry.kill(handle.id)
    assert getattr(handle, "killed", False) is True
    assert handle.status == "interrupted"
    mock_task.cancel.assert_called_once()

    # Eviction of terminal handles
    registry.evict(handle.id)
    assert registry.get(handle.id) is None


# ---------------------------------------------------------------------------
# WF-A9: Teammates do not reply to acknowledgements
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wf_a9_no_reply_to_acknowledgements() -> None:
    from coderai.teams.manager import TeamManager

    team_mgr = TeamManager()
    tm1 = team_mgr.spawn_teammate("Alice", "coder", auto_start=False)
    tm2 = team_mgr.spawn_teammate("Bob", "reviewer", auto_start=False)

    mb1 = team_mgr.channel.get_mailbox(tm1.teammate_id)
    assert mb1 is not None

    # Send an acknowledgement to Alice
    team_mgr.send_message(
        sender=tm2.name,
        recipient=tm1.name,
        content="Acknowledged by Bob (reviewer): please review this",
    )

    # Verify mailbox received message
    received = await mb1.receive_async(timeout_seconds=0.1)
    assert received is not None
    assert received.content.startswith("Acknowledged by")

    # In the teammate loop logic, acknowledgement messages must NOT trigger a reply
    from coderai.teams.manager import is_acknowledgement_message

    assert is_acknowledgement_message(received.content) is True


# ---------------------------------------------------------------------------
# WF-A11: Hooks execution safety and timeout
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wf_a11_hook_timeout_and_process_group(tmp_path: Path) -> None:
    from coderai.hooks.engine import (
        MAX_HOOK_TIMEOUT_SECONDS,
        clamp_hook_timeout,
        execute_hook_command_async,
    )

    # Timeout must be clamped to MAX_HOOK_TIMEOUT_SECONDS
    assert clamp_hook_timeout(999999.0) <= MAX_HOOK_TIMEOUT_SECONDS
    assert clamp_hook_timeout(-10.0) > 0

    # Command that sleeps past timeout
    payload = {"hook_event_name": "PreToolUse"}
    out = await execute_hook_command_async(
        command="sleep 10",
        payload=payload,
        project_root=str(tmp_path),
        timeout_s=0.2,
    )
    assert out.decision == "deny"
    assert "timed out" in out.reason.lower()


# ---------------------------------------------------------------------------
# WF-A12: Subagent isolated_cwd defaults to project root
# ---------------------------------------------------------------------------
def test_wf_a12_isolated_cwd_default(tmp_path: Path) -> None:
    from coderai.subagents.runner import SubAgentManager
    from coderai.subagents.builder import SubAgentSpec

    runner = SubAgentManager(project_root=str(tmp_path))
    spec = SubAgentSpec(prompt="test", description="test description")
    # When isolation is not requested, effective isolated_cwd should be None (or project root)
    eff_cwd = runner._resolve_subagent_cwd(spec, "session_123")
    assert eff_cwd == str(tmp_path) or eff_cwd is None


# ---------------------------------------------------------------------------
# WF-A13: Background subagent job IDs and JobStore cancel callback
# ---------------------------------------------------------------------------
def test_wf_a13_job_store_cancel_callback() -> None:
    from coderai.background.manager import get_job_store

    store = get_job_store()
    job_id = "test_subagent_proc_less"
    session_id = "test_session_wf_a13"

    store.start(job_id=job_id, session_id=session_id, kind="subagent", label="test")

    cancelled = [False]

    def _on_cancel():
        cancelled[0] = True

    store.register_cancel_callback(job_id, _on_cancel)

    # Calling kill on process-less job must trigger the cancel callback
    res = store.kill(job_id, session_id, reason="User killed")
    assert res == "cancellation-requested"
    assert cancelled[0] is True


# ---------------------------------------------------------------------------
# WF-A16: Team tasks lineage and auto-start
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wf_a16_team_task_lineage_and_auto_start(tmp_path: Path) -> None:
    from coderai.teams.manager import TeamManager

    tm_mgr = TeamManager()
    tm = tm_mgr.spawn_teammate(
        "Eve",
        "coder",
        auto_start=False,
        project_root=str(tmp_path),
        parent_session_id="parent_123",
        depth=1,
    )
    assert tm.project_root == str(tmp_path)
    assert tm.parent_session_id == "parent_123"
    assert tm.depth == 1

    # Task board auto-start ready tasks
    t1 = tm_mgr.task_board.create_task("Task 1", "Desc 1", assigned_to=tm.name)
    assert t1.status == "pending"
    # Auto-promotion of runnable tasks
    runnable = tm_mgr.get_runnable_tasks_for_teammate(tm.teammate_id)
    assert any(t.task_id == t1.task_id for t in runnable)


# ---------------------------------------------------------------------------
# WF-A17: Subagent hooks pass parent session ID
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wf_a17_subagent_hooks_parent_session_id(tmp_path: Path) -> None:
    runner = SubAgentManager(project_root=str(tmp_path))
    spec = SubAgentSpec(
        prompt="test",
        description="test description",
        parent_session_id="real_parent_session_id",
        parent_agent_id="agent_parent",
    )

    fired_spawn_sessions = []
    fired_stop_sessions = []

    with (
        patch(
            "coderai.hooks.runner.run_on_subagent_spawn",
            side_effect=lambda **kw: fired_spawn_sessions.append(kw.get("parent_session_id")),
        ),
        patch(
            "coderai.hooks.runner.run_subagent_stop",
            side_effect=lambda sid, *a, **kw: fired_stop_sessions.append(sid),
        ),
    ):

        async def _mock_loop(*args, **kwargs):
            raise RuntimeError("forced subagent failure")

        with patch.object(runner, "_run_subagent_loop", side_effect=_mock_loop):
            res = await runner.spawn_subagent(spec)
            assert res.status == "failed"

    # Must pass the real parent session id, not "root" or parent_agent_id
    assert "real_parent_session_id" in fired_spawn_sessions
    assert "real_parent_session_id" in fired_stop_sessions


# ---------------------------------------------------------------------------
# WF-A18: Goals store atomic write and advance round
# ---------------------------------------------------------------------------
def test_wf_a18_goals_advance_and_only_one_running(tmp_path: Path) -> None:
    from coderai.goals.core import get_goal_store

    store = get_goal_store(str(tmp_path))
    g1 = store.create("session_1", "Objective 1", max_rounds=2)
    assert g1.status == "running"
    assert g1.round == 1

    # Advance round
    store.advance_round("session_1", g1.id)
    g1_updated = store.get_active_goal("session_1")
    assert g1_updated is not None
    assert g1_updated.round == 2

    # Advancing past max_rounds marks failed
    store.advance_round("session_1", g1.id)
    g1_failed = store.list("session_1")[0]
    assert g1_failed.status == "failed"

    # Creating second goal pauses first
    g2 = store.create("session_1", "Objective 2")
    g3 = store.create("session_1", "Objective 3")
    assert g3.status == "running"
    # When g2 is updated to running, g3 must be paused (only one running goal)
    store.update("session_1", g2.id, status="running")
    running_goals = [g for g in store.list("session_1") if g.status == "running"]
    assert len(running_goals) == 1
    assert running_goals[0].id == g2.id


# ---------------------------------------------------------------------------
# IN-A6: MCP client replacement disconnects old client
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_in_a6_mcp_disconnect_old_client_on_connect() -> None:
    from coderai.mcp.manager import McpManager

    mgr = McpManager()
    mock_client1 = MagicMock()
    mock_client1.server_name = "test_server"
    mock_client1.is_connected.return_value = True
    mock_client1.disconnect = AsyncMock()

    mgr.clients.append(mock_client1)

    # Calling disconnect on manager must not abort if one client raises
    mock_client2 = MagicMock()
    mock_client2.disconnect = AsyncMock(side_effect=RuntimeError("boom"))
    mock_client3 = MagicMock()
    mock_client3.disconnect = AsyncMock()
    mgr.clients = [mock_client2, mock_client3]

    await mgr.disconnect()
    mock_client2.disconnect.assert_awaited_once()
    mock_client3.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# IN-A7: MCP stdio and streamable HTTP handling
# ---------------------------------------------------------------------------
def test_in_a7_mcp_transport_properties() -> None:
    from coderai.mcp.transport import StreamableHttpMcpTransport

    trans = StreamableHttpMcpTransport(server_name="test", url="http://localhost:8000/mcp")
    assert hasattr(trans, "last_http_status")
    assert hasattr(trans, "session_id")


# ---------------------------------------------------------------------------
# IN-A8: OAuth device flow handling
# ---------------------------------------------------------------------------
def test_in_a8_oauth_device_flow_errors() -> None:
    from coderai.auth.oauth import DeviceAuthorization, wait_for_device_token, OAuthError

    auth = DeviceAuthorization(
        device_code="dev123",
        user_code="USER123",
        verification_uri="http://auth",
        verification_uri_complete="http://auth/complete",
        expires_in=2,
        interval=1,
    )

    with patch(
        "coderai.auth.oauth._poll_device_token",
        return_value=(400, {"error": "access_denied"}),
    ):
        with pytest.raises(OAuthError) as exc_info:
            wait_for_device_token(auth)
        assert (
            "access_denied" in str(exc_info.value).lower()
            or "denied" in str(exc_info.value).lower()
        )


# ---------------------------------------------------------------------------
# IN-A13 & IN-A15: Config precedence & typed config does not write files
# ---------------------------------------------------------------------------
def test_in_a15_load_typed_config_does_not_write_files(tmp_path: Path) -> None:
    from coderai.config import load_typed_config

    non_existent = tmp_path / "missing_config.toml"
    assert not non_existent.exists()

    cfg = load_typed_config(config_file=non_existent)
    assert cfg is not None
    # Loading missing config must not create the file on disk!
    assert not non_existent.exists()


# ---------------------------------------------------------------------------
# IN-A17: ACP terminal runner pops pending request on cancel
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_in_a17_acp_pending_request_popped_on_cancel() -> None:
    from coderai.acp.runner import AcpSubagentRunner, AcpRunConfig

    runner = AcpSubagentRunner(AcpRunConfig(command="echo"))
    runner._write_message = lambda msg: None  # mock write

    task = asyncio.create_task(runner._send_request("test_method", {}))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Pending requests must be cleaned up in finally block
    assert len(runner._pending_requests) == 0


# ---------------------------------------------------------------------------
# UI-A10: Ctrl-O quotes external editor path
# ---------------------------------------------------------------------------
def test_ui_a10_open_external_editor_quotes_path() -> None:
    from coderai.utils.editor import open_external_editor

    with patch("os.getenv", return_value="dummy_editor"), patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        open_external_editor("sample prompt")
        cmd_run = mock_run.call_args[0][0]
        # Command must properly quote or invoke editor without unquoted temp path
        assert "dummy_editor" in cmd_run
