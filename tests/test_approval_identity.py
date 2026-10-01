"""Approval identity must survive collisions and owner shutdown."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from coderai.approval_runtime.models import ApprovalSource
from coderai.approval_runtime.runtime import ApprovalCancelledError, ApprovalRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("other_owner", ["owner", "other"], ids=["same-owner", "cross-owner"])
async def test_collision_keeps_existing_waiter_owner_and_events(other_owner):
    runtime = ApprovalRuntime()
    events = []
    wire = []
    runtime.subscribe(events.append)
    runtime.bind_root_wire_hub(
        SimpleNamespace(publish_nowait=lambda event, **kw: wire.append((event, kw)))
    )
    original = runtime.create_request(
        request_id="explicit",
        tool_call_id="first",
        action="write",
        description="first",
        source=ApprovalSource(kind="turn", id="owner", session_id="owner"),
    )
    started = asyncio.Event()

    async def wait():
        started.set()
        return await runtime.wait_for_response(original.id)

    waiter = asyncio.create_task(wait())
    await started.wait()
    try:
        with pytest.raises(ValueError, match="already exists"):
            runtime.create_request(
                request_id="explicit",
                tool_call_id="second",
                action="write",
                description="second",
                source=ApprovalSource(kind="turn", id=other_owner, session_id=other_owner),
            )
        assert runtime.get_request("explicit") is original
        assert len(events) == len(wire) == 1
        if other_owner != "owner":
            runtime.clear_session_grants(other_owner)
            assert runtime.list_pending() == [original]
        runtime.resolve("explicit", "approve", "original decision")
        assert await asyncio.wait_for(waiter, 1) == ("approve", "original decision")
        assert wire[-1][1]["session_id"] == "owner"
    finally:
        runtime.clear_session_grants()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("settlement", ["resolved", "cancelled"])
def test_settled_ids_cannot_be_reused_for_stale_responses(settlement):
    runtime = ApprovalRuntime()
    args = dict(
        request_id="id",
        tool_call_id="call",
        action="write",
        description="write",
        source=ApprovalSource(kind="turn", id="owner"),
    )
    original = runtime.create_request(**args)
    if settlement == "resolved":
        runtime.resolve("id", "reject")
    else:
        runtime.cancel("id")
    with pytest.raises(ValueError, match="already exists"):
        runtime.create_request(**args)
    assert runtime.get_request("id") is original
    assert runtime.resolve("id", "approve").response == "reject"


@pytest.mark.asyncio
async def test_collision_does_not_redirect_owner_cancellation():
    runtime = ApprovalRuntime()
    original = runtime.create_request(
        request_id="id",
        tool_call_id="call",
        action="write",
        description="write",
        source=ApprovalSource(kind="turn", id="owner"),
    )
    with pytest.raises(ValueError):
        runtime.create_request(
            request_id="id",
            tool_call_id="other",
            action="write",
            description="write",
            source=ApprovalSource(kind="turn", id="other"),
        )
    assert runtime.cancel_by_source("turn", "other") == 0
    assert runtime.cancel_by_source("turn", "owner") == 1
    with pytest.raises(ApprovalCancelledError):
        await runtime.wait_for_response(original.id)
