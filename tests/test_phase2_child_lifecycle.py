"""Actual foreground spawn handles remain cancellable only while running."""

from __future__ import annotations

import asyncio
import sys

import pytest

from coderai.cli.session_factory import close_session_manager
from coderai.soul.session.manager import SessionManager
from coderai.subagents.builder import SubAgentSpec
from coderai.subagents.core import AgentRegistry
from coderai.subagents.runner import SubAgentManager, make_subagent_result


@pytest.fixture
def blocked_children(tmp_path, monkeypatch):
    registry = AgentRegistry()
    monkeypatch.setattr(sys.modules["coderai.subagents.core"], "_registry", registry)
    manager = SubAgentManager(str(tmp_path), create_openai_client=lambda: {})
    entered = asyncio.Queue()
    aborts = {}

    async def blocked(spec, sid, abort, events):
        aborts[sid] = abort
        await entered.put(sid)
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_run_subagent_loop", blocked)
    return manager, registry, entered, aborts


@pytest.mark.parametrize("operation", ["kill", "interrupt", "cancel_subagent"])
async def test_foreground_cancellation_wakes_inflight_invocation(blocked_children, operation):
    manager, registry, entered, aborts = blocked_children
    spec = SubAgentSpec(description="blocked", prompt="wait", parent_session_id="owner")
    task = asyncio.create_task(manager.spawn_subagent(spec))
    sid = await asyncio.wait_for(entered.get(), 1)
    handle = registry.get(spec.task_id)
    assert handle.manager is manager
    assert handle.task is task
    try:
        if operation == "cancel_subagent":
            manager.cancel_subagent(sid)
        else:
            getattr(registry, operation)(handle.id)
        result = await asyncio.wait_for(task, 1)
        assert result.status == handle.status == "interrupted"
        assert aborts[sid].is_set()
        assert handle.task is None
        assert sid not in manager._active_controllers
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_completed_child_does_not_retain_the_continuing_owner_task(
    blocked_children, monkeypatch
):
    manager, registry, _, _ = blocked_children
    spec = SubAgentSpec(description="quick", prompt="done")
    settled = asyncio.Event()
    release = asyncio.Event()

    async def complete(spec, sid, abort, events):
        return make_subagent_result(spec, sid, "completed", summary="done")

    monkeypatch.setattr(manager, "_run_subagent_loop", complete)

    async def owner():
        assert (await manager.spawn_subagent(spec)).status == "completed"
        settled.set()
        await release.wait()
        return "owner survived"

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(settled.wait(), 1)
        handle = registry.get(spec.task_id)
        assert handle.task is None
        registry.kill(handle.id)
        assert not task.done()
        release.set()
        assert await asyncio.wait_for(task, 1) == "owner survived"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_owner_close_cancels_real_foreground_child_and_preserves_sibling(
    blocked_children, tmp_path, isolated_home
):
    child_manager, registry, entered, aborts = blocked_children
    owner = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {},
        get_resolved_settings=lambda: {},
    )
    owner._active_session_id = "owner"
    tasks = [
        asyncio.create_task(
            child_manager.spawn_subagent(
                SubAgentSpec(description=sid, prompt="wait", parent_session_id=sid)
            )
        )
        for sid in ("owner", "sibling")
    ]
    try:
        await asyncio.wait_for(entered.get(), 1)
        await asyncio.wait_for(entered.get(), 1)
        await close_session_manager(owner)
        result = await asyncio.wait_for(tasks[0], 1)
        assert result.status == "interrupted"
        sibling = registry.list(parent_session_id="sibling")[0]
        assert not tasks[1].done() and not sibling.killed
        assert not aborts[sibling.run_session_id].is_set()
        assert registry.list(parent_session_id="owner") == []
    finally:
        for handle in registry.list():
            registry.kill(handle.id)
        await asyncio.gather(*tasks, return_exceptions=True)
        owner.dispose()
