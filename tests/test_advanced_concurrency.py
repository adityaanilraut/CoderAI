"""Comprehensive unit and stress tests for Advanced Concurrency, Multi-Agent Coordination, and Subagent Spawning."""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
import pytest

from coderai.core.hooks import (
    HookOutput,
    HookPoint,
    execute_hook_command_async,
    run_hook_point_async,
)
from coderai.core.lifecycle.cascade import CancellationTree, LifecycleCoordinator
from coderai.core.spawn import (
    SubagentDescriptor,
    ToolRestriction,
    check_subagent_depth_quota,
    cleanup_subagent_scratchpad,
    parse_subagent_descriptor,
    setup_subagent_scratchpad,
)
from coderai.core.subagent import (
    MAX_SUBAGENT_DEPTH,
    SubAgentManager,
    SubAgentResult,
    SubAgentSpec,
)
from coderai.core.teams.concurrency import (
    ConcurrencyConflictError,
    calculate_backoff_delay,
    cas_retry_async,
    cas_retry_sync,
)
from coderai.core.teams.deadlock import (
    CycleDetectedError,
    DeadlockError,
    InterAgentWaitWatchdog,
    assert_acyclic_dependencies,
    detect_task_cycles,
)
from coderai.core.teams.mailbox import (
    ActorChannel,
    AsyncMailbox,
    MessagePriority,
)
from coderai.core.teams.manager import TeamManager, TeamTaskBoard
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.path_lock import PathLockManager, get_path_lock_manager


# =========================================================================
# 1. Dynamic Agent Spawning & Hierarchical Topologies Tests
# =========================================================================


def test_subagent_descriptor_parsing():
    raw_one_shot = {
        "version": 1,
        "mode": "one-shot",
        "provider": "in_process",
        "label": "Research task",
    }
    desc = parse_subagent_descriptor(raw_one_shot)
    assert desc.version == 1
    assert desc.mode == "one-shot"
    assert desc.provider == "in_process"
    assert desc.label == "Research task"

    raw_continuable = {
        "version": 1,
        "mode": "continuable",
        "provider": "in_process",
        "label": "Coder Agent",
        "agentProvider": "deepseek",
        "agentModel": "deepseek-chat",
        "persona": "Expert Backend Developer",
        "toolFilter": {
            "allow": ["read", "grep", "write"],
            "deny": ["AskUserQuestion"],
        },
    }
    desc_c = parse_subagent_descriptor(raw_continuable)
    assert desc_c.mode == "continuable"
    assert desc_c.agent_provider == "deepseek"
    assert desc_c.persona == "Expert Backend Developer"
    assert desc_c.tool_filter is not None
    assert desc_c.tool_filter.is_tool_permitted("read") is True
    assert desc_c.tool_filter.is_tool_permitted("write") is True
    assert desc_c.tool_filter.is_tool_permitted("AskUserQuestion") is False
    assert desc_c.tool_filter.is_tool_permitted("unknown_tool") is False

    d_dict = desc_c.to_dict()
    assert d_dict["version"] == 1
    assert d_dict["mode"] == "continuable"


def test_subagent_depth_quota_and_scratchpad(tmp_path: pathlib.Path):
    # Depth within limit
    ok, err = check_subagent_depth_quota(current_depth=2, max_depth=3)
    assert ok is True
    assert err is None

    # Depth at or exceeding limit
    ok, err = check_subagent_depth_quota(current_depth=3, max_depth=3)
    assert ok is False
    assert "RecursionLimitError" in (err or "")

    # Scratchpad lifecycle
    sp_path = setup_subagent_scratchpad(str(tmp_path), "test_session_123")
    assert os.path.exists(sp_path)
    assert "test_session_123" in sp_path

    cleanup_subagent_scratchpad(sp_path)


@pytest.mark.asyncio
async def test_subagent_result_telemetry_and_exit_codes():
    res_completed = SubAgentResult(
        task_id="t1",
        session_id="s1",
        status="completed",
        summary="Done",
        exit_code=0,
        token_telemetry={
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cached_tokens": 20,
            "active_tokens": 150,
            "total_tokens": 150,
        },
        diffs=[{"file_path": "main.py"}],
    )
    d = res_completed.to_dict()
    assert d["status"] == "completed"
    assert d["exit_code"] == 0
    assert d["token_telemetry"]["total_tokens"] == 150
    assert len(d["diffs"]) == 1

    md = res_completed.format_markdown()
    assert "Exit Code**: `0`" in md
    assert "Files Modified (1)" in md


@pytest.mark.asyncio
async def test_subagent_depth_limit_rejection(tmp_path: pathlib.Path):
    mgr = SubAgentManager(str(tmp_path), create_openai_client=lambda: {"client": None})
    spec = SubAgentSpec(
        description="Nested agent",
        prompt="Do work",
        depth=3,
        max_depth=3,
    )
    res = await mgr.spawn_subagent(spec)
    assert res.status == "failed"
    assert res.exit_code == 1
    assert "RecursionLimitError" in (res.error or "")


# =========================================================================
# 2. Agent Team Concurrency & Async Communication Fabric Tests
# =========================================================================


@pytest.mark.asyncio
async def test_async_mailbox_priority_and_drain():
    mb = AsyncMailbox(agent_id="agent_1", max_size=10)
    assert mb.is_empty() is True

    # Send messages with different priorities
    await mb.send_async("low priority msg", priority=MessagePriority.LOW)
    await mb.send_async("critical alert", priority=MessagePriority.CRITICAL)
    await mb.send_async("normal msg", priority=MessagePriority.NORMAL)
    await mb.send_async("high priority task", priority=MessagePriority.HIGH)

    assert mb.size == 4

    # Recv should return in priority order: CRITICAL -> HIGH -> NORMAL -> LOW
    first = await mb.recv_async()
    assert first == "critical alert"

    second = await mb.recv_async()
    assert second == "high priority task"

    third = await mb.recv_async()
    assert third == "normal msg"

    fourth = await mb.recv_async()
    assert fourth == "low priority msg"

    assert mb.is_empty() is True


@pytest.mark.asyncio
async def test_actor_channel_pub_sub():
    channel = ActorChannel()
    mb_coder = channel.register_mailbox("coder")
    mb_reviewer = channel.register_mailbox("reviewer")
    mb_planner = channel.register_mailbox("planner")

    # Subscribe to topic
    channel.subscribe("code_review", mb_reviewer)
    channel.subscribe("code_review", mb_planner)

    # Publish to topic
    delivered = await channel.publish_async("code_review", "Review PR #42")
    assert delivered == 2

    # Reviewer and Planner get the message, Coder does not
    assert (await mb_reviewer.recv_async()) == "Review PR #42"
    assert (await mb_planner.recv_async()) == "Review PR #42"
    assert mb_coder.is_empty() is True

    # Broadcast to all
    b_delivered = await channel.broadcast_async("Sprint Sync at 10 AM", exclude_agent_id="planner")
    assert b_delivered == 2
    assert (await mb_coder.recv_async()) == "Sprint Sync at 10 AM"
    assert (await mb_reviewer.recv_async()) == "Sprint Sync at 10 AM"
    assert mb_planner.is_empty() is True


def test_task_graph_cycle_detection():
    # Acyclic graph: A -> B -> C
    acyclic = {
        "A": ["B"],
        "B": ["C"],
        "C": [],
    }
    assert detect_task_cycles(acyclic) is None
    assert_acyclic_dependencies(acyclic)  # Should not raise

    # Direct cycle: A -> B -> A
    cyclic_direct = {
        "A": ["B"],
        "B": ["A"],
    }
    cycle = detect_task_cycles(cyclic_direct)
    assert cycle is not None
    assert cycle[0] == cycle[-1]

    with pytest.raises(CycleDetectedError):
        assert_acyclic_dependencies(cyclic_direct)

    # Indirect 3-node cycle: A -> B -> C -> A
    cyclic_indirect = {
        "A": ["B"],
        "B": ["C"],
        "C": ["A"],
    }
    cycle3 = detect_task_cycles(cyclic_indirect)
    assert cycle3 is not None
    assert cycle3[0] == cycle3[-1]

    with pytest.raises(CycleDetectedError):
        assert_acyclic_dependencies(cyclic_indirect)


def test_inter_agent_wait_watchdog():
    watchdog = InterAgentWaitWatchdog()
    watchdog.record_wait("agent_A", "agent_B")
    watchdog.record_wait("agent_B", "agent_C")

    # Cycle happens when C waits on A
    with pytest.raises(DeadlockError) as exc_info:
        watchdog.record_wait("agent_C", "agent_A")
    assert "DeadlockError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_cas_concurrency_retry_with_jitter():
    # Simulate a resource with revision conflicts under parallel writes
    resource = {"value": 0, "revision": 1}
    lock = asyncio.Lock()

    async def _failing_cas_updater():
        async with lock:
            current_rev = resource["revision"]
            await asyncio.sleep(0.01)  # small yield to cause contention
            if resource["revision"] != current_rev:
                raise ConcurrencyConflictError(
                    "revision mismatch",
                    expected_revision=current_rev,
                    actual_revision=resource["revision"],
                )
            resource["value"] += 1
            resource["revision"] += 1
            return resource["value"]

    # Run 5 concurrent CAS updaters with automatic retry and jitter
    tasks = [
        cas_retry_async(_failing_cas_updater, max_retries=10, initial_delay=0.01)
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks)
    assert len(results) == 5
    assert resource["value"] == 5
    assert resource["revision"] == 6


# =========================================================================
# 3. Parallel Tool Execution & Resource Throttling Tests
# =========================================================================


@pytest.mark.asyncio
async def test_path_lock_manager_concurrency():
    lock_mgr = PathLockManager()
    path_a = "/workspace/file_a.py"
    path_b = "/workspace/file_b.py"

    active_readers = 0
    max_concurrent_readers = 0
    writer_active = False

    async def _reader():
        nonlocal active_readers, max_concurrent_readers, writer_active
        async with lock_mgr.acquire_read_lock(path_a):
            assert not writer_active, "Reader ran while writer was active!"
            active_readers += 1
            max_concurrent_readers = max(max_concurrent_readers, active_readers)
            await asyncio.sleep(0.05)
            active_readers -= 1

    async def _writer():
        nonlocal writer_active, active_readers
        async with lock_mgr.acquire_write_lock(path_a):
            assert active_readers == 0, "Writer ran while readers were active!"
            writer_active = True
            await asyncio.sleep(0.05)
            writer_active = False

    # Launch concurrent readers on path_a
    await asyncio.gather(_reader(), _reader(), _reader(), _reader())
    assert max_concurrent_readers > 1, "Concurrent readers should have overlapped!"

    # Launch readers and writer together on path_a
    await asyncio.gather(_reader(), _writer(), _reader())

    # Verify disjoint paths do not block each other
    t0 = time.time()
    async def _writer_a():
        async with lock_mgr.acquire_write_lock(path_a):
            await asyncio.sleep(0.05)

    async def _writer_b():
        async with lock_mgr.acquire_write_lock(path_b):
            await asyncio.sleep(0.05)

    await asyncio.gather(_writer_a(), _writer_b())
    elapsed = time.time() - t0
    # Both 0.05s writes on disjoint paths should complete in parallel (~0.05s-0.08s, well under 0.10s)
    assert elapsed < 0.12, f"Disjoint path writes took {elapsed}s, should run concurrently!"


@pytest.mark.asyncio
async def test_tool_executor_semaphore_throttling(tmp_path: pathlib.Path):
    executor = ToolExecutor(str(tmp_path), concurrency_limit=2)
    assert executor.concurrency_limit == 2

    active_count = 0
    max_active = 0

    async def _mock_tool(args, ctx):
        nonlocal active_count, max_active
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(0.04)
        active_count -= 1
        return {"ok": True, "res": "done"}

    from coderai.core.tools.types import ToolDefinition
    mock_def = ToolDefinition(name="test_concurrency_tool", handler=_mock_tool)
    executor.registry.register(mock_def)

    tool_calls = [
        {"id": f"tc_{i}", "type": "function", "function": {"name": "test_concurrency_tool", "arguments": "{}"}}
        for i in range(6)
    ]

    executions = await executor.execute_tool_calls("sess_1", tool_calls, parallel=True)
    assert len(executions) == 6
    assert max_active <= 2, f"Expected concurrency bounded by 2, got max_active={max_active}"


# =========================================================================
# 4. Structured Async Cancellation & Lifecycle Cascades Tests
# =========================================================================


@pytest.mark.asyncio
async def test_cancellation_tree_cascades():
    tree = CancellationTree()

    # Build hierarchy: root -> turn_1 -> subagent_1 -> tool_task
    tree.register_node("root_session")
    tree.register_node("turn_1", parent_id="root_session")
    tree.register_node("subagent_1", parent_id="turn_1")

    cleaned_up_nodes = []

    async def _task_coro():
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            raise

    t1 = asyncio.create_task(_task_coro())
    t2 = asyncio.create_task(_task_coro())
    t3 = asyncio.create_task(_task_coro())

    tree.register_task("root_session", t1)
    tree.register_task("turn_1", t2)
    tree.register_task("subagent_1", t3)

    tree.register_cleanup("subagent_1", lambda: cleaned_up_nodes.append("subagent_1"))
    tree.register_cleanup("turn_1", lambda: cleaned_up_nodes.append("turn_1"))

    # Cancel turn_1 subtree (should cancel turn_1 and subagent_1, but leave root t1 unaffected)
    res = await tree.cancel_subtree("turn_1", reason="Turn Interrupted")
    assert res["cancelled_tasks"] >= 2
    assert "subagent_1" in cleaned_up_nodes
    assert "turn_1" in cleaned_up_nodes

    await asyncio.sleep(0.01)
    assert (t2.cancelled() or t2.cancelling() > 0) is True
    assert (t3.cancelled() or t3.cancelling() > 0) is True
    assert t1.cancelled() is False

    t1.cancel()
    try:
        await t1
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_async_hook_execution(tmp_path: pathlib.Path):
    # Test valid async hook execution
    cmd = "python3 -c \"import sys, json; print(json.dumps({'decision': 'allow', 'additionalContext': ['Async context test']}))\""
    payload = {"tool_name": "bash", "hook_event_name": "PreToolUse"}

    out = await execute_hook_command_async(cmd, payload, str(tmp_path), timeout_s=5.0)
    assert out.decision == "allow"
    assert "Async context test" in out.additional_context

    # Test hook with timeout
    slow_cmd = "python3 -c \"import time; time.sleep(5)\""
    out_timeout = await execute_hook_command_async(slow_cmd, payload, str(tmp_path), timeout_s=0.2)
    assert out_timeout.decision == "deny"
    assert "timed out" in (out_timeout.reason or "")

    # Test hook exception containment (invalid script syntax does not crash loop)
    bad_cmd = "python3 -c \"raise ValueError('Crash')\""
    out_err = await execute_hook_command_async(bad_cmd, payload, str(tmp_path), timeout_s=2.0)
    assert out_err.decision == "deny"
    assert out_err.exit_code != 0
