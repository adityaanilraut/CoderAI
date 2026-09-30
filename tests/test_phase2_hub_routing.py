"""Runtime-owned root events cannot reach another session's wire client."""

from __future__ import annotations

import asyncio

import pytest

from coderai.approval_runtime import ApprovalSource
from coderai.soul.session.manager import SessionManager
from coderai.wire.server import WireServer
from coderai.wire.types import ApprovalRequest, ApprovalResponse, TurnEnd


@pytest.fixture
def manager(tmp_path, isolated_home):
    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {},
        get_resolved_settings=lambda: {},
    )
    yield manager
    manager.dispose()


@pytest.mark.parametrize("settlement", ["resolve", "cancel"])
async def test_actual_shared_manager_servers_route_approval_feedback_to_owner(manager, settlement):
    hub = manager.root_wire_hub
    servers = [WireServer(manager, sid) for sid in ("sessionA", "sessionB")]
    tasks = []
    for server in servers:
        server._initialized = True
        server._hub_queue = hub.subscribe(session_id=server._session_id)
        tasks.append(asyncio.create_task(server._hub_loop()))
    try:
        runtime = manager.approval_runtime
        record = runtime.create_request(
            tool_call_id="call-A",
            action="write",
            description="private A action",
            source=ApprovalSource(kind="foreground_turn", id="sessionA"),
        )
        if settlement == "resolve":
            runtime.resolve(record.id, "reject", feedback="private A feedback")
        else:
            runtime.cancel(record.id, feedback="private A feedback")
        hub.publish_nowait(TurnEnd(), session_id="sessionB")
        event_a = await asyncio.wait_for(servers[0]._write_queue.get(), 1)
        event_b = await asyncio.wait_for(servers[1]._write_queue.get(), 1)
        assert event_a["params"]["type"] == "ApprovalResponse"
        assert event_a["params"]["payload"]["request_id"] == record.id
        assert event_a["params"]["payload"]["feedback"] == "private A feedback"
        assert event_b["params"]["type"] == "TurnEnd"
        assert servers[0]._write_queue.empty() and servers[1]._write_queue.empty()
    finally:
        hub.shutdown()
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("explicit_owner", [False, True])
async def test_background_request_captures_runtime_owner_for_later_resolution(
    manager, explicit_owner
):
    hub = manager.root_wire_hub
    owner = hub.subscribe(session_id="sessionA")
    sibling = hub.subscribe(session_id="sessionB")
    runtime = manager.approval_runtime
    source = ApprovalSource(
        kind="background_agent",
        id="background-task",
        agent_id="worker",
        session_id=None if explicit_owner else "sessionA",
    )
    record = runtime.create_request(
        tool_call_id="call",
        action="bash",
        description="background action",
        source=source,
        **({"session_id": "sessionA"} if explicit_owner else {}),
    )
    assert record.session_id == "sessionA"
    request = owner.get_nowait()
    assert isinstance(request, ApprovalRequest) and request.id == record.id
    runtime.resolve(record.id, "approve", feedback="owner feedback")
    response = owner.get_nowait()
    assert isinstance(response, ApprovalResponse) and response.feedback == "owner feedback"
    assert sibling.empty()
    owner.close()
    sibling.close()


async def test_ownerless_runtime_and_hub_events_fail_closed_for_session_clients(manager):
    hub = manager.root_wire_hub
    servers = [WireServer(manager, sid) for sid in ("sessionA", "sessionB")]
    observer = hub.subscribe()
    tasks = []
    for server in servers:
        server._initialized = True
        server._hub_queue = hub.subscribe(session_id=server._session_id)
        tasks.append(asyncio.create_task(server._hub_loop()))
    try:
        runtime = manager.approval_runtime
        record = runtime.create_request(
            tool_call_id="unknown",
            action="bash",
            description="ownerless action",
            source=ApprovalSource(kind="background_agent", id="unowned-task"),
        )
        assert record.session_id is None
        runtime.resolve(record.id, "reject", feedback="ownerless private feedback")
        hub.publish_nowait(ApprovalResponse(request_id="unknown-direct", feedback="private"))
        assert isinstance(observer.get_nowait(), ApprovalRequest)
        assert isinstance(observer.get_nowait(), ApprovalResponse)
        assert isinstance(observer.get_nowait(), ApprovalResponse)
        for server in servers:
            hub.publish_nowait(TurnEnd(), session_id=server._session_id)
            event = await asyncio.wait_for(server._write_queue.get(), 1)
            assert event["params"]["type"] == "TurnEnd"
            assert server._write_queue.empty()
    finally:
        observer.close()
        hub.shutdown()
        await asyncio.gather(*tasks)


async def test_out_of_turn_notification_routes_to_owning_wire_server(manager):
    hub = manager.root_wire_hub
    servers = [WireServer(manager, sid) for sid in ("sessionA", "sessionB")]
    tasks = []
    for server in servers:
        server._initialized = True
        server._hub_queue = hub.subscribe(session_id=server._session_id)
        tasks.append(asyncio.create_task(server._hub_loop()))
    try:
        manager.notify("private A notice", session_id="sessionA", targets=["wire"])
        manager.notify("ownerless notice", payload={"sessionId": "sessionB"}, targets=["wire"])
        hub.publish_nowait(TurnEnd(), session_id="sessionB")
        event_a = await asyncio.wait_for(servers[0]._write_queue.get(), 1)
        event_b = await asyncio.wait_for(servers[1]._write_queue.get(), 1)
        assert event_a["params"]["type"] == "Notification"
        assert event_a["params"]["payload"]["title"] == "private A notice"
        assert event_a["params"]["payload"]["payload"]["sessionId"] == "sessionA"
        assert event_b["params"]["type"] == "TurnEnd"
        assert servers[0]._write_queue.empty() and servers[1]._write_queue.empty()
    finally:
        hub.shutdown()
        await asyncio.gather(*tasks)


async def test_live_notification_uses_bound_runtime_owner_without_root_duplicates(manager):
    from coderai.wire.emitter import bind_emitter, reset_emitter
    from coderai.wire.types import Notification

    a_emitter = manager.get_event_emitter("sessionA")
    b_emitter = manager.get_event_emitter("sessionB")
    a = a_emitter.ui_side(merge=False, replay=False)
    b = b_emitter.ui_side(merge=False, replay=False)
    root = manager.root_wire_hub.subscribe()
    manager._running_sessions.update({"sessionA", "sessionB"})
    token = bind_emitter(a_emitter)
    try:
        manager._active_session_id = "sessionB"
        view = manager.notify("bound A notice", targets=["wire"])
        assert view.event.payload["sessionId"] == "sessionA"
        assert isinstance(a.try_receive_nowait(), Notification)
        assert b.try_receive_nowait() is None and root.empty()
        manager.notify("explicit B notice", session_id="sessionB", targets=["wire"])
        assert b.try_receive_nowait().title == "explicit B notice"
        assert a.try_receive_nowait() is None and root.empty()
        manager.notify("LLM only", session_id="sessionA", targets=["llm"])
        assert a.try_receive_nowait() is None and root.empty()
    finally:
        reset_emitter(token)
        manager._running_sessions.clear()
        a.close()
        b.close()
        root.close()


async def test_private_notification_is_not_claimed_by_another_sessions_llm(manager):
    a = manager._create_empty_session()
    b = manager._create_empty_session()
    private = manager.notify("private A", "private A body", session_id=a, targets=["llm"])
    generic = manager.notify("system notice", "generic body", targets=["llm"])
    await manager._deliver_llm_notifications(b)
    b_messages = manager.list_session_messages(b)
    assert any("generic body" in message.content for message in b_messages)
    assert not any("private A" in message.content for message in b_messages)
    assert (
        manager.notification_manager.store.merged_view(private.event.id)
        .delivery.sinks["llm"]
        .status
        == "pending"
    )
    assert (
        manager.notification_manager.store.merged_view(generic.event.id)
        .delivery.sinks["llm"]
        .status
        == "acked"
    )
    await manager._deliver_llm_notifications(a)
    assert any("private A body" in message.content for message in manager.list_session_messages(a))
