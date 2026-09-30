"""Protocol regressions through real runtime managers and mocked providers."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from kosong.message import TextPart as InputText

from coderai.acp.engine import SessionManagerEngine
from coderai.soul import RunCancelled
from coderai.soul.session.manager import SessionManager
from coderai.wire.emitter import get_emitter
from coderai.wire.types import TextPart, ThinkPart, ToolCall, ToolResult, TurnBegin, TurnEnd


def response(text="", *, thinking=None, calls=None):
    return {
        "choices": [
            {"message": {"content": text, "reasoning_content": thinking, "tool_calls": calls}}
        ]
    }


def manager(root, provider, **callbacks):
    root.mkdir(exist_ok=True)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=provider)))
    return SessionManager(
        project_root=str(root),
        create_openai_client=lambda: {"client": client, "model": "gpt-4o"},
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "mergeAllAvailableSkills": False,
            "permissions": {"defaultMode": "allowAll", "allow": ["read-in-cwd"]},
        },
        **callbacks,
    )


async def collect(engine, text="hello", cancel=None):
    async with asyncio.timeout(10):
        return [
            event async for event in engine.run([InputText(text=text)], cancel or asyncio.Event())
        ]


def texts(events):
    return "".join(event.text for event in events if isinstance(event, TextPart))


@pytest.mark.asyncio
async def test_real_manager_full_answer_thinking_and_tool_events(tmp_path):
    calls = [
        {
            "type": "function",
            "id": "call1",
            "function": {"name": "read", "arguments": '{"file_path":"missing"}'},
        }
    ]
    replies = iter([response("checking", thinking="reasoning", calls=calls), response("answer")])
    mgr = manager(tmp_path / "project", lambda **kw: next(replies))
    engine = SessionManagerEngine(mgr)
    events = await collect(engine)
    assert texts(events) == "checkinganswer"
    assert [event.text for event in events if isinstance(event, ThinkPart)] == ["reasoning"]
    assert [event.id for event in events if isinstance(event, ToolCall)] == ["call1"]
    results = [event for event in events if isinstance(event, ToolResult)]
    assert len(results) == 1 and results[0].tool_call_id == "call1"
    assert results[0].return_value.is_error
    assert isinstance(events[0], TurnBegin) and isinstance(events[-1], TurnEnd)
    assert mgr.get_event_emitter(engine.session_id)._local._raw_queue.subscriber_count == 0
    mgr.dispose()


@pytest.mark.asyncio
async def test_real_streaming_no_final_duplicate_and_callback_single_delivery(tmp_path):
    def provider(**kwargs):
        return iter(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="one", reasoning_content="think")
                        )
                    ]
                ),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="two"))]),
            ]
        )

    callback_chunks = []

    def callback(text):
        callback_chunks.append(text)
        get_emitter().text(text)

    mgr = manager(tmp_path / "project", provider, on_stream_chunk=callback)
    engine = SessionManagerEngine(mgr)
    events = await collect(engine)
    assert texts(events) == "onetwo"
    assert callback_chunks == ["one", "two"]
    assert "".join(event.text for event in events if isinstance(event, ThinkPart)) == "think"
    mgr.dispose()


@pytest.mark.asyncio
async def test_real_managers_interleaved_events_and_replay_are_isolated(tmp_path):
    barrier = threading.Barrier(2)

    def provider(secret):
        def create(**kwargs):
            barrier.wait(timeout=5)
            return response(secret, thinking=f"think-{secret}")

        return create

    managers = [manager(tmp_path / str(i), provider(f"private-{i}")) for i in range(2)]
    engines = [SessionManagerEngine(mgr) for mgr in managers]
    events = await asyncio.wait_for(asyncio.gather(*(collect(engine) for engine in engines)), 10)
    for i, (mgr, engine, stream) in enumerate(zip(managers, engines, events)):
        assert texts(stream) == f"private-{i}"
        buffered = mgr.get_event_emitter(engine.session_id).buffered()
        assert texts(buffered) == f"private-{i}"
        mgr.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [False, True])
async def test_real_manager_cancel_first_and_later_turn(tmp_path, later):
    mgr = manager(tmp_path / "project", lambda **kw: response("first"))
    engine = SessionManagerEngine(mgr)
    if later:
        await collect(engine)
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    mgr._create_completion_with_retry = blocked
    cancel = asyncio.Event()
    task = asyncio.create_task(collect(engine, cancel=cancel))
    await asyncio.wait_for(entered.wait(), 5)
    assert engine.session_id is not None and mgr.get_session(engine.session_id) is not None
    cancel.set()
    with pytest.raises(RunCancelled):
        await asyncio.wait_for(task, 5)
    assert stopped.is_set()
    assert mgr.get_session(engine.session_id).status == "interrupted"
    assert mgr.get_event_emitter(engine.session_id)._local._raw_queue.subscriber_count == 0
    mgr._create_completion_with_retry = _reply
    resumed = await collect(engine, "after cancel")
    assert texts(resumed) == "resumed"
    mgr.dispose()


async def _reply(*args, **kwargs):
    return response("resumed")


@pytest.mark.asyncio
async def test_real_manager_callback_full_message_only_once(tmp_path):
    callbacks = []

    def callback(message, committed):
        callbacks.append(message.content)
        if message.role == "assistant":
            get_emitter().text(message.content)

    mgr = manager(
        tmp_path / "project", lambda **kw: response("answer"), on_assistant_message=callback
    )
    events = await collect(SessionManagerEngine(mgr))
    assert texts(events) == "answer"
    assert callbacks == ["answer"]
    mgr.dispose()


@pytest.mark.asyncio
async def test_real_manager_acp_client_receives_answer_thinking_and_tool_status(tmp_path):
    import acp
    from coderai.acp.session import ACPSession

    calls = [
        {
            "type": "function",
            "id": "call",
            "function": {"name": "read", "arguments": '{"file_path":"missing"}'},
        }
    ]
    replies = iter([response("checking", thinking="reasoning", calls=calls), response("answer")])
    mgr = manager(tmp_path / "project", lambda **kw: next(replies))
    updates = []

    async def update(**kwargs):
        updates.append(kwargs["update"])

    session = ACPSession("acp", SessionManagerEngine(mgr), SimpleNamespace(session_update=update))
    result = await session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])
    assert result.stop_reason == "end_turn"
    assert (
        "".join(
            item.content.text for item in updates if item.session_update == "agent_message_chunk"
        )
        == "checkinganswer"
    )
    assert (
        "".join(
            item.content.text for item in updates if item.session_update == "agent_thought_chunk"
        )
        == "reasoning"
    )
    assert [item.status for item in updates if item.session_update == "tool_call_update"] == [
        "failed"
    ]
    mgr.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_id", [None, "explicit-session"])
async def test_real_manager_wire_server_isolated_event_source_and_tail(tmp_path, explicit_id):
    from coderai.wire.server import WireServer

    mgr = manager(
        tmp_path / "project", lambda **kw: response("wire answer", thinking="wire thought")
    )
    server = WireServer(mgr, explicit_id)
    server._initialized = True
    result = await server._handle_prompt("prompt", {"user_input": "hi"})
    assert result["result"]["status"] == "finished"
    if explicit_id:
        assert server._session_id == explicit_id and mgr.get_session(explicit_id) is not None
    frames = []
    while not server._write_queue.empty():
        frames.append(server._write_queue.get_nowait())
    events = [frame["params"] for frame in frames if frame.get("method") == "event"]
    assert any(
        event["type"] == "TextPart" and event["payload"]["text"] == "wire answer"
        for event in events
    )
    assert events[-1]["type"] == "TurnEnd"
    replay = await server._handle_replay("replay", {})
    assert replay["result"]["events"] == len(events)
    mgr.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [False, True])
async def test_real_manager_wire_cancel_first_and_later(tmp_path, later):
    from coderai.wire.server import WireServer

    mgr = manager(tmp_path / "project", lambda **kw: response("first"))
    server = WireServer(mgr)
    server._initialized = True
    if later:
        await server._handle_prompt("first", {"user_input": "hi"})
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    mgr._create_completion_with_retry = blocked
    task = asyncio.create_task(server._handle_prompt("prompt", {"user_input": "hi"}))
    await asyncio.wait_for(entered.wait(), 5)
    assert server._session_id and mgr.get_session(server._session_id)
    await server._handle_cancel("cancel", {})
    result = await asyncio.wait_for(task, 5)
    assert result["result"]["status"] == "cancelled"
    assert mgr.get_session(server._session_id).status == "interrupted"
    assert mgr.get_event_emitter(server._session_id)._local._raw_queue.subscriber_count == 0
    mgr.dispose()


@pytest.mark.asyncio
async def test_real_manager_acp_cancel_permission_pause_closes_adapter(tmp_path):
    import acp
    from coderai.acp.session import ACPSession

    call = {
        "type": "function",
        "id": "call",
        "function": {
            "name": "bash",
            "arguments": '{"command":"true","sandbox_permissions":"danger-full-access","justification":"need"}',
        },
    }
    mgr = manager(tmp_path / "project", lambda **kw: response(calls=[call]))
    entered = asyncio.Event()

    async def permission(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    async def update(**kwargs):
        pass

    engine = SessionManagerEngine(mgr)
    session = ACPSession(
        "acp", engine, SimpleNamespace(session_update=update, request_permission=permission)
    )
    task = asyncio.create_task(
        session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])
    )
    await asyncio.wait_for(entered.wait(), 5)
    await session.cancel()
    result = await asyncio.wait_for(task, 5)
    assert result.stop_reason == "cancelled"
    assert not engine._running
    assert mgr.get_event_emitter(engine.session_id)._local._raw_queue.subscriber_count == 0
    mgr.dispose()


def test_closed_event_stream_ignores_late_provider_publication():
    from coderai.wire.emitter import WireEmitter

    emitter = WireEmitter()
    emitter.close()
    emitter.text("late private text")
    assert emitter.buffered() == []


@pytest.mark.asyncio
async def test_concurrent_prompt_same_session_rejects_before_history_mutation(tmp_path):
    mgr = manager(tmp_path / "project", lambda **kw: response("first"))
    engine = SessionManagerEngine(mgr)
    await collect(engine)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    mgr._create_completion_with_retry = blocked
    cancel = asyncio.Event()
    task = asyncio.create_task(collect(engine, cancel=cancel))
    await entered.wait()
    before = mgr.list_session_messages(engine.session_id)
    with pytest.raises(RuntimeError, match="active turn"):
        await mgr.reply_session(engine.session_id, "must not append")
    assert mgr.list_session_messages(engine.session_id) == before
    cancel.set()
    with pytest.raises(RunCancelled):
        await task
    mgr.dispose()


@pytest.mark.asyncio
async def test_producer_overflow_exception_is_observed(tmp_path):
    import gc
    from coderai.utils.broadcast import BroadcastQueueOverflow
    from coderai.wire import Wire
    from coderai.wire.emitter import WireEmitter

    emitter = WireEmitter()
    emitter._local = Wire(history_limit=0, queue_limit=1)
    subscriber = emitter.ui_side(merge=False, replay=False)
    engine = SessionManagerEngine(SimpleNamespace())
    failures = []
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: failures.append(context))

    async def overflow():
        emitter.send(TurnBegin())
        emitter.send(TurnEnd())

    try:
        with pytest.raises(BroadcastQueueOverflow):
            async for _ in engine._drive(overflow(), subscriber):
                pass
        subscriber.close()
        gc.collect()
        await asyncio.sleep(0)
        assert not failures
    finally:
        loop.set_exception_handler(original)
        emitter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [False, True])
async def test_provider_thread_cancellation_suppresses_late_output(tmp_path, later):
    mgr = manager(tmp_path / "project", lambda **kw: response("first"))
    engine = SessionManagerEngine(mgr)
    if later:
        await collect(engine)
    entered = threading.Event()
    release = threading.Event()
    settled = threading.Event()

    def stream():
        try:
            entered.set()
            release.wait(timeout=5)
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="late private text"))]
            )
        finally:
            settled.set()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: stream()))
    )
    mgr.create_openai_client = lambda: {"client": client, "model": "gpt-4o"}
    cancel = asyncio.Event()
    task = asyncio.create_task(collect(engine, cancel=cancel))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        cancel.set()
        with pytest.raises(RunCancelled):
            await asyncio.wait_for(task, 5)
        emitter = mgr.get_event_emitter(engine.session_id)
        count = emitter.event_count(TextPart)
        release.set()
        assert await asyncio.to_thread(settled.wait, 5)
        await asyncio.sleep(0)
        assert emitter.event_count(TextPart) == count
        assert "late private text" not in texts(emitter.buffered())
    finally:
        release.set()
        mgr.dispose()


@pytest.mark.asyncio
async def test_real_sdk_clients_receive_only_their_managers_answers(
    tmp_path, monkeypatch, isolated_home
):
    from pathlib import Path

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "sdks" / "coderai-sdk" / "src")
    )
    from coderai_sdk import CoderAIClient

    barrier = threading.Barrier(2)

    def provider(label):
        def complete(**kwargs):
            barrier.wait(timeout=5)
            return response(f"PRIVATE SDK {label} ANSWER", thinking=f"PRIVATE SDK {label} THOUGHT")

        return complete

    managers = [manager(tmp_path / label, provider(label)) for label in ("A", "B")]
    clients = [CoderAIClient(engine=SessionManagerEngine(mgr)) for mgr in managers]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(client.prompt("answer") for client in clients)), 10
        )
        for label, result, mgr in zip(("A", "B"), results, managers, strict=True):
            assert result.text == f"PRIVATE SDK {label} ANSWER"
            assert result.thinking == f"PRIVATE SDK {label} THOUGHT"
            assert any(
                message.content == result.text
                for message in mgr.list_session_messages(result.session_id)
                if message.role == "assistant"
            )
    finally:
        for mgr in managers:
            mgr.dispose()


@pytest.mark.asyncio
async def test_wire_forwarder_ready_before_immediate_turn():
    from coderai.wire.emitter import WireEmitter
    from coderai.wire.server import WireServer

    class ImmediateManager:
        def __init__(self):
            self.emitter = WireEmitter()
            self.sessions = {}

        def get_event_emitter(self, session_id):
            return self.emitter

        async def create_session(self, text, *, session_id):
            # No await: this turn settles before any other task can run.
            self.emitter.turn_begin(text)
            self.emitter.text("immediate answer")
            self.emitter.turn_end()
            self.sessions[session_id] = SimpleNamespace(status="completed")
            return session_id

        def get_session(self, session_id):
            return self.sessions.get(session_id)

    mgr = ImmediateManager()
    server = WireServer(mgr)
    server._initialized = True
    try:
        result = await server._handle_prompt("prompt", {"user_input": "hi"})
        assert result["result"]["status"] == "finished"
        frames = []
        while not server._write_queue.empty():
            frames.append(server._write_queue.get_nowait())
        assert [frame["params"]["type"] for frame in frames] == ["TurnBegin", "TextPart", "TurnEnd"]
        assert frames[1]["params"]["payload"]["text"] == "immediate answer"
        assert mgr.emitter._local._raw_queue.subscriber_count == 0
    finally:
        mgr.emitter.close()
        server._fallback_emitter.close()


@pytest.mark.asyncio
async def test_wire_real_manager_immediate_missing_provider_keeps_error_events(tmp_path):
    from coderai.wire.server import WireServer

    mgr = manager(tmp_path / "project", lambda **kwargs: response("unused"))
    mgr.create_openai_client = lambda: {"client": None, "model": "gpt-4o"}
    server = WireServer(mgr)
    server._initialized = True
    try:
        result = await server._handle_prompt("prompt", {"user_input": "hi"})
        assert "error" in result and "LLM is not set" in result["error"]["message"]
        frames = []
        while not server._write_queue.empty():
            frames.append(server._write_queue.get_nowait())
        assert [frame["params"]["type"] for frame in frames] == ["TurnBegin", "TextPart", "TurnEnd"]
        assert "API key not found" in frames[1]["params"]["payload"]["text"]
        assert mgr.get_event_emitter(server._session_id)._local._raw_queue.subscriber_count == 0
    finally:
        mgr.dispose()
        server._fallback_emitter.close()
