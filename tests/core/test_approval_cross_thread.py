"""Regression: approval replies from the UI thread must wake the agent loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coderAI.tui.controller import UIBridge


@pytest.mark.asyncio
async def test_submit_command_from_other_loop_resolves_approval_waiter() -> None:
    """UI-thread submit_command must hop onto the agent loop.

    Reproducing the stall: agent awaits an approval Future on loop A; the
    Textual UI used to call submit_command on loop B, so set_result never
    woke loop A until a later enqueue_command (e.g. user typing "yo").
    """
    agent = SimpleNamespace(
        config=SimpleNamespace(approval_timeout_seconds=5, max_iterations=50),
        tools=None,
        tracker_info=None,
    )
    bridge = UIBridge(agent, on_event=MagicMock())
    ready = threading.Event()
    result: dict[str, object] = {}

    def _agent_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run() -> None:
            bridge._loop = asyncio.get_running_loop()
            ready.set()
            result["approved"] = await bridge.request_tool_approval(
                tool_id="cross-thread-1",
                tool_name="run_command",
                arguments={"command": "echo hi"},
            )

        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    worker = threading.Thread(target=_agent_thread, name="agent-loop-test", daemon=True)
    worker.start()
    assert ready.wait(timeout=2), "agent loop never became ready"

    # Give the agent coroutine time to park on the approval Future.
    for _ in range(50):
        if "cross-thread-1" in bridge._approval_waiters:
            break
        await asyncio.sleep(0.01)
    assert "cross-thread-1" in bridge._approval_waiters

    await bridge.submit_command(
        "tool_approval_resp",
        toolId="cross-thread-1",
        approve=True,
    )

    worker.join(timeout=2)
    assert not worker.is_alive(), "agent thread still blocked after approval"
    assert result.get("approved") is True


@pytest.mark.asyncio
async def test_enqueue_approval_resp_wakes_agent_loop() -> None:
    agent = SimpleNamespace(
        config=SimpleNamespace(approval_timeout_seconds=5, max_iterations=50),
        tools=None,
        tracker_info=None,
    )
    bridge = UIBridge(agent, on_event=MagicMock())
    ready = threading.Event()
    result: dict[str, object] = {}

    def _agent_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run() -> None:
            bridge._loop = asyncio.get_running_loop()
            ready.set()
            result["approved"] = await bridge.request_tool_approval(
                tool_id="enqueue-1",
                tool_name="run_command",
                arguments={"command": "echo hi"},
            )

        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    worker = threading.Thread(target=_agent_thread, name="agent-loop-enqueue", daemon=True)
    worker.start()
    assert ready.wait(timeout=2)

    for _ in range(50):
        if "enqueue-1" in bridge._approval_waiters:
            break
        await asyncio.sleep(0.01)

    bridge.enqueue_command(
        "tool_approval_resp",
        toolId="enqueue-1",
        approve=True,
    )

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result.get("approved") is True
