"""Bounded replay and explicit lifetime of live wire subscriptions."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coderai.utils.aioqueue import QueueShutDown
from coderai.utils.broadcast import BroadcastQueue, BroadcastQueueOverflow
from coderai.wire import Wire
from coderai.wire.emitter import WireEmitter
from coderai.wire.root_hub import RootWireHub
from coderai.wire.server import WireServer
from coderai.wire.types import (
    ApprovalRequest,
    QuestionNotSupported,
    QuestionRequest,
    TurnBegin,
    TurnEnd,
)


def test_replay_is_bounded_and_live_only_subscriptions_skip_history():
    queue = BroadcastQueue[int](history_limit=3, queue_limit=5)
    for value in range(600):
        queue.publish_nowait(value)
    assert queue.history_size == 3
    with queue.subscribe() as replay:
        assert [replay.get_nowait() for _ in range(3)] == [597, 598, 599]
    with queue.subscribe(replay=False) as live:
        assert live.empty()
    assert queue.subscriber_count == 0


def test_repeated_wire_turns_release_subscribers_and_bound_replay():
    wire = Wire(history_limit=4, queue_limit=8)
    for index in range(600):
        with wire.ui_side(merge=False, replay=False) as raw:
            with wire.ui_side(merge=True, replay=False) as merged:
                wire.soul_side.send(TurnBegin(user_input=str(index)))
                wire.soul_side.send(TurnEnd())
                assert isinstance(raw.try_receive_nowait(), TurnBegin)
                assert isinstance(merged.try_receive_nowait(), TurnBegin)
        assert wire._raw_queue.subscriber_count == 0
        assert wire._merged_queue.subscriber_count == 0
    assert wire._raw_queue.history_size == wire._merged_queue.history_size == 4


def test_sync_overflow_is_explicit_for_control_messages_and_other_subscribers():
    queue = BroadcastQueue[ApprovalRequest](history_limit=0, queue_limit=1)
    stalled = queue.subscribe()
    healthy = queue.subscribe()
    first, second = ApprovalRequest(id="first"), ApprovalRequest(id="second")
    queue.publish_nowait(first)
    assert healthy.get_nowait() is first
    with pytest.raises(BroadcastQueueOverflow):
        queue.publish_nowait(second)
    assert stalled.closed and stalled.qsize() == 0
    with pytest.raises(BroadcastQueueOverflow):
        stalled.get_nowait()
    assert healthy.get_nowait() is second
    assert queue.subscriber_count == 1
    healthy.close()


def test_wire_nowait_does_not_hide_overflow_as_an_empty_queue():
    wire = Wire(history_limit=0, queue_limit=1)
    subscriber = wire.ui_side(merge=False, replay=False)
    wire.soul_side.send(TurnBegin())
    with pytest.raises(BroadcastQueueOverflow):
        wire.soul_side.send(ApprovalRequest(id="control"))
    with pytest.raises(BroadcastQueueOverflow):
        subscriber.try_receive_nowait()
    assert wire._raw_queue.subscriber_count == 0


def test_emitter_close_releases_retained_events_and_closes_streams():
    async def run():
        emitter = WireEmitter()
        raw = emitter.ui_side(merge=False, replay=False)
        merged = emitter.ui_side(merge=True, replay=False)
        emitter.send(TurnBegin())
        emitter._tool_results.add("tool")
        emitter.attach_session_wire(Wire())
        emitter.close()
        emitter.close()
        assert emitter.buffered() == []
        assert emitter._tool_results == set() and emitter._event_counts == {}
        assert emitter._session_wire is None and emitter._loop is None
        assert emitter._local._raw_queue.subscriber_count == 0
        assert emitter._local._merged_queue.subscriber_count == 0
        assert raw._queue.empty() and merged._queue.empty()
        with pytest.raises(QueueShutDown):
            await raw.receive()
        with pytest.raises(QueueShutDown):
            await merged.receive()

    asyncio.run(run())


def test_late_provider_sends_do_not_repopulate_closed_emitter():
    async def run():
        emitter = WireEmitter()
        emitter.ui_side(merge=False)
        emitter.send(TurnBegin())
        emitter.close()
        await asyncio.to_thread(emitter.send, TurnBegin(user_input="late worker"))
        emitter._send(TurnEnd())
        assert emitter.buffered() == []
        assert emitter._event_counts == {} and emitter._tool_results == set()
        assert emitter._local._raw_queue.history_size == 0
        assert emitter._local._merged_queue.history_size == 0

    asyncio.run(run())


@pytest.mark.parametrize("immediate", [False, True])
def test_direct_wire_late_native_parts_do_not_retain_merge_buffer(immediate):
    from kosong.message import TextPart

    wire = Wire()
    wire.soul_side.send(TextPart(text="before close"))
    wire.shutdown(immediate=immediate)
    for _ in range(600):
        wire.soul_side.send(TextPart(text="x" * 100))
    wire.soul_side.flush()
    assert wire.soul_side._merge_buffer is None
    assert wire._raw_queue.history_size == wire._merged_queue.history_size == 0


def test_root_hub_routes_only_known_owner_to_scoped_clients():
    hub = RootWireHub(queue_limit=2)
    with hub.subscribe(session_id="A") as a, hub.subscribe(session_id="B") as b:
        with hub.subscribe() as observer:
            unknown = TurnBegin(user_input="unknown")
            owned = TurnBegin(user_input="A only")
            hub.publish_nowait(unknown)
            hub.publish_nowait(owned, session_id="A")
            assert a.get_nowait() is owned
            assert a.empty() and b.empty()
            assert observer.get_nowait() is unknown
            assert observer.get_nowait() is owned
    assert hub.subscriber_count == 0 and not hub._routes
    with pytest.raises(ValueError):
        hub.subscribe(session_id="")


def test_root_hub_stalled_other_session_does_not_fail_owner_delivery():
    hub = RootWireHub(queue_limit=1)
    a = hub.subscribe(session_id="A")
    b = hub.subscribe(session_id="B")
    hub.publish_nowait(TurnBegin(), session_id="B")
    hub.publish_nowait(TurnEnd(), session_id="A")
    assert isinstance(a.get_nowait(), TurnEnd)
    assert not b.closed and b.qsize() == 1
    hub.shutdown()


def test_async_publish_backpressures_and_close_unblocks_the_publisher():
    async def run():
        queue = BroadcastQueue[int](history_limit=0, queue_limit=1)
        async with queue.subscribe() as subscriber:
            await queue.publish(1)
            pending = asyncio.create_task(queue.publish(2))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not pending.done() and subscriber.qsize() == 1
            assert await subscriber.get() == 1
            await asyncio.wait_for(pending, 1)
            assert await subscriber.get() == 2
            await queue.publish(3)
            blocked = asyncio.create_task(queue.publish(4))
            await asyncio.sleep(0)
            subscriber.close()
            await asyncio.wait_for(blocked, 1)
        assert queue.subscriber_count == 0

    asyncio.run(run())


def test_graceful_shutdown_preserves_full_queues_then_explicit_close_drains():
    queue = BroadcastQueue[int](history_limit=0, queue_limit=2)
    subscriber = queue.subscribe()
    queue.publish_nowait(1)
    queue.publish_nowait(2)
    queue.shutdown()
    assert queue.subscriber_count == 0 and subscriber.closed
    assert subscriber.get_nowait() == 1
    subscriber.close()
    assert subscriber.empty()
    with pytest.raises(QueueShutDown):
        subscriber.get_nowait()


def test_root_hub_bounds_control_delivery_and_wakes_closed_consumers():
    async def run():
        hub = RootWireHub(queue_limit=1)
        subscriber = hub.subscribe()
        hub.publish_nowait(ApprovalRequest(id="first"))
        with pytest.raises(BroadcastQueueOverflow):
            hub.publish_nowait(ApprovalRequest(id="second"))
        with pytest.raises(BroadcastQueueOverflow):
            await subscriber.get()
        assert hub.subscriber_count == 0
        active = hub.subscribe()
        waiting = asyncio.create_task(active.get())
        await asyncio.sleep(0)
        hub.shutdown()
        with pytest.raises(QueueShutDown):
            await waiting
        with pytest.raises(QueueShutDown):
            hub.publish_nowait(TurnEnd())
        assert hub.subscriber_count == 0

    asyncio.run(run())


def test_root_forwarder_overflow_aborts_connection_and_rejects_control():
    async def run():
        hub = RootWireHub(queue_limit=1)
        interrupted = []
        server = WireServer(SimpleNamespace(interrupt_session=interrupted.append), "session")
        server._hub_queue = hub.subscribe()
        pending = ApprovalRequest(id="pending")
        server._pending[pending.id] = pending
        reader = asyncio.create_task(asyncio.Event().wait())
        turn = asyncio.create_task(asyncio.Event().wait())
        server._reader_task, server._turn_task = reader, turn
        hub.publish_nowait(TurnBegin())
        with pytest.raises(BroadcastQueueOverflow):
            hub.publish_nowait(TurnEnd())
        with pytest.raises(BroadcastQueueOverflow):
            await server._hub_loop()
        assert await pending.wait() == "reject"
        assert interrupted == ["session"]
        results = await asyncio.gather(reader, turn, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert hub.subscriber_count == 0

    asyncio.run(run())


def test_wire_writer_backpressure_and_writer_failure_wake_blocked_senders():
    async def run():
        server = WireServer(SimpleNamespace())
        assert server._write_queue.maxsize > 0
        for index in range(server._write_queue.maxsize):
            server._write_queue.put_nowait({"id": index})
        waiting = asyncio.create_task(server._send({"id": "blocked"}))
        await asyncio.sleep(0)
        assert not waiting.done()

        def fail(_line):
            raise OSError("fixture disconnected writer")

        server._write_stdout = fail
        await server._write_loop()
        await asyncio.wait_for(waiting, 1)
        assert server._write_queue.empty()

    asyncio.run(run())


def test_disconnect_rejects_control_registered_during_task_cleanup():
    async def run():
        server = WireServer(SimpleNamespace())
        late = ApprovalRequest(id="late")

        async def closing_turn():
            try:
                await asyncio.Future()
            finally:
                server._pending[late.id] = late

        async def eof():
            await asyncio.sleep(0)

        task = asyncio.create_task(closing_turn())
        server._dispatch_tasks.add(task)
        server._read_loop = eof
        await server.serve()
        assert await late.wait() == "reject"
        assert not server._pending

    asyncio.run(run())


def test_unsupported_questions_do_not_accumulate_pending_requests():
    async def run():
        server = WireServer(SimpleNamespace())
        task = server._start_event_forwarding()
        assert task is not None
        for index in range(600):
            question = QuestionRequest(id=str(index))
            server._fallback_emitter.send(question)
            with pytest.raises(QuestionNotSupported):
                await question.wait()
            assert not server._pending
        task.cancel()
        await task

    asyncio.run(run())


def test_recorder_failure_closes_its_subscription():
    async def run():
        async def fail(_message):
            raise OSError("fixture disk failure")

        wire = Wire(file_backend=SimpleNamespace(append_message=fail))
        wire.soul_side.send(TurnBegin())
        assert wire._recorder is not None
        with pytest.raises(OSError, match="fixture disk failure"):
            await wire._recorder._task
        assert wire._merged_queue.subscriber_count == 0
        wire.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("started", [False, True])
def test_forwarder_early_exit_closes_subscription_even_before_first_step(started):
    async def run():
        emitter = WireEmitter()
        server = WireServer(SimpleNamespace())
        server._fallback_emitter = emitter
        for _ in range(600):
            task = server._start_event_forwarding()
            assert task is not None
            if started:
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            assert emitter._local._raw_queue.subscriber_count == 0

    asyncio.run(run())


def test_forwarder_overflow_rejects_pending_and_reports_failure():
    async def run():
        emitter = WireEmitter()
        emitter._local = Wire(history_limit=0, queue_limit=1)
        interrupted = []
        server = WireServer(SimpleNamespace(interrupt_session=interrupted.append), "session")
        server._fallback_emitter = emitter
        pending = ApprovalRequest(id="awaiting")
        server._pending[pending.id] = pending
        task = server._start_event_forwarding()
        assert task is not None
        emitter.send(TurnBegin())
        with pytest.raises(BroadcastQueueOverflow):
            emitter.send(ApprovalRequest(id="lost"))
        with pytest.raises(BroadcastQueueOverflow):
            await task
        assert await pending.wait() == "reject"
        assert interrupted == ["session"]
        assert not server._pending
        assert emitter._local._raw_queue.subscriber_count == 0

    asyncio.run(run())


def test_python312_queue_shutdown_handles_bounded_waiters(monkeypatch):
    # Exercise the compatibility implementation even on Python 3.13+.
    from coderai.utils import aioqueue

    namespace = {"__name__": "queue312_fixture"}
    source = Path(aioqueue.__file__).read_text()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "version_info", (3, 12))
        exec(compile(source, str(aioqueue.__file__), "exec"), namespace)
    queue_type, shutdown_error = namespace["Queue"], namespace["QueueShutDown"]

    async def run():
        queue = queue_type(maxsize=1)
        queue.put_nowait(1)
        putter = asyncio.create_task(queue.put(2))
        await asyncio.sleep(0)
        queue.shutdown()
        with pytest.raises(shutdown_error):
            await putter
        assert queue.get_nowait() == 1
        with pytest.raises(shutdown_error):
            await queue.get()
        empty = queue_type(maxsize=1)
        getter = asyncio.create_task(empty.get())
        await asyncio.sleep(0)
        empty.shutdown()
        with pytest.raises(shutdown_error):
            await getter
        retained = queue_type(maxsize=1)
        retained.put_nowait(1)
        retained.shutdown()
        retained.shutdown(immediate=True)
        assert retained.empty()

    asyncio.run(run())
