"""Durable ACP replay, isolated forks, and session identity boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import acp
import pytest

from coderai.acp.server import ACPServer
from coderai.acp.session import ACPSession, _current_turn_id
from coderai.session import Session
from coderai.session_state import save_session_state
from coderai.wire.types import TextPart, TurnBegin, TurnEnd


@pytest.fixture(autouse=True)
def session_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path / "share"))


@pytest.mark.asyncio
async def test_create_rejects_duplicate_without_truncating_history(tmp_path):
    session = await Session.create(str(tmp_path), session_id="existing")
    session.context_file.write_text('{"role":"user","content":"keep"}\n')
    with pytest.raises(FileExistsError):
        await Session.create(str(tmp_path), session_id="existing")
    assert "keep" in session.context_file.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["../escape", "/absolute", "..", "", "a\\b", "a\x00b"])
async def test_session_ids_never_escape_store(tmp_path, session_id):
    with pytest.raises(ValueError):
        await Session.create(str(tmp_path), session_id=session_id)
    assert await Session.find(str(tmp_path), session_id) is None


@pytest.mark.asyncio
async def test_prompt_persists_replay_before_transport_delivery(tmp_path):
    stored = await Session.create(str(tmp_path))

    async def run(user_input, cancel_event):
        yield TurnBegin(user_input=user_input)
        yield TextPart(text="durable answer")
        yield TurnEnd()

    conn = SimpleNamespace(session_update=AsyncMock())
    live = ACPSession(stored.id, SimpleNamespace(run=run, session=stored), conn)
    await live.prompt([acp.schema.TextContentBlock(type="text", text="question")])
    records = stored.wire_file.replay_sync()
    assert len(records) == 3
    conn.session_update.reset_mock()
    await live.replay_history(stored.wire_file)
    updates = [call.kwargs["update"] for call in conn.session_update.await_args_list]
    assert [update.session_update for update in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]
    assert updates[-1].content.text == "durable answer"
    assert len(stored.wire_file.replay_sync()) == 3  # replay must not record itself


@pytest.mark.asyncio
async def test_partial_prompt_is_replayable_after_transport_failure(tmp_path):
    stored = await Session.create(str(tmp_path))

    async def run(user_input, cancel_event):
        yield TurnBegin(user_input=user_input)
        yield TextPart(text="partial answer")
        yield TextPart(text="not delivered")

    conn = SimpleNamespace(session_update=AsyncMock(side_effect=RuntimeError("private-secret")))
    live = ACPSession(stored.id, SimpleNamespace(run=run, session=stored), conn)
    with pytest.raises(acp.RequestError) as error:
        await live.prompt([acp.schema.TextContentBlock(type="text", text="question")])
    assert "private-secret" not in str(error.value)
    assert any(
        getattr(event, "text", None) == "partial answer" for event in stored.wire_file.replay_sync()
    )
    assert live._turn_state is None
    assert _current_turn_id.get() is None


@pytest.mark.asyncio
async def test_stream_close_failure_still_clears_turn_context():
    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            raise RuntimeError("close failed")

    live = ACPSession("id", SimpleNamespace(run=lambda *args: Stream()), SimpleNamespace())
    with pytest.raises(RuntimeError, match="close failed"):
        await live.prompt([])
    assert live._turn_state is None
    assert _current_turn_id.get() is None


@pytest.mark.asyncio
async def test_busy_prompt_is_invalid_request():
    started = asyncio.Event()
    release = asyncio.Event()

    async def run(user_input, cancel_event):
        started.set()
        await release.wait()
        yield TurnEnd()

    live = ACPSession("id", SimpleNamespace(run=run), SimpleNamespace())
    task = asyncio.create_task(live.prompt([]))
    await started.wait()
    try:
        with pytest.raises(acp.RequestError) as error:
            await live.prompt([])
        assert error.value.code == -32600
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_global_list_and_fork_preserve_facade_history(tmp_path):
    server = ACPServer()
    server.conn = SimpleNamespace(session_update=AsyncMock())
    server.client_capabilities = acp.schema.ClientCapabilities()
    server._check_auth = lambda: None
    new = await server.new_session(str(tmp_path))
    parent = server.sessions[new.session_id][0].cli.session
    engine = server.sessions[new.session_id][0].cli
    sid = await engine.manager.create_session("parent", skills=[])
    engine.bind_session(sid)
    from coderai.acp.session import save_engine_session_id

    save_engine_session_id(parent.dir, sid)
    parent.context_file.write_text('{"role":"user","content":"parent"}\n')
    parent.wire_file.append_message_sync(TurnBegin(user_input="parent"))
    parent.wire_file.append_message_sync(TextPart(text="answer"))
    parent.state.plan_mode = True
    save_session_state(parent.state, parent.dir)
    fork = await server.fork_session(str(tmp_path), new.session_id)
    child = server.sessions[fork.session_id][0].cli.session
    assert child.context_file.read_bytes() == parent.context_file.read_bytes()
    assert child.wire_file.path.read_bytes() == parent.wire_file.path.read_bytes()
    assert child.state.plan_mode is True
    child.state.plan_mode = False
    assert parent.state.plan_mode is True
    listed = await server.list_sessions()
    assert {row.session_id for row in listed.sessions} == {new.session_id, fork.session_id}
    assert all(row.cwd == str(tmp_path) for row in listed.sessions)


@pytest.mark.asyncio
async def test_set_default_mode_validates_session():
    with pytest.raises(acp.RequestError) as error:
        await ACPServer().set_session_mode("default", "unknown")
    assert error.value.code == -32602


@pytest.mark.asyncio
async def test_four_modes_are_effective_isolated_and_restored(tmp_path):
    server = ACPServer()
    server.conn = SimpleNamespace(session_update=AsyncMock())
    server.client_capabilities = acp.schema.ClientCapabilities()
    server._check_auth = lambda: None
    first = await server.new_session(str(tmp_path))
    second = await server.new_session(str(tmp_path))
    assert [mode.id for mode in first.modes.available_modes] == ["default", "plan", "auto", "yolo"]
    first_engine = server.sessions[first.session_id][0].cli
    second_engine = server.sessions[second.session_id][0].cli
    for mode in ["plan", "auto", "yolo", "default", "auto"]:
        changed = await server.set_config_option("mode", first.session_id, mode)
        assert first_engine._plan_mode is (mode == "plan")
        assert first_engine.manager.is_afk() is (mode == "auto")
        assert first_engine.manager.is_yolo() is (mode == "yolo")
        assert changed.config_options[-1].root.current_value == mode
        assert second_engine.manager.is_afk() is False
        assert second_engine.manager.is_yolo() is False
    server.sessions.pop(first.session_id)
    resumed = await server.resume_session(str(tmp_path), first.session_id)
    assert resumed.modes.current_mode_id == "auto"
    assert server.sessions[first.session_id][0].cli.manager.is_afk() is True
    with pytest.raises(acp.RequestError):
        await server.set_config_option("mode", first.session_id, "unknown")


def test_capability_aware_configuration_options():
    from coderai.acp.config_options import config_options
    from coderai.config import LLMModel
    from coderai.session_state import SessionState

    config = SimpleNamespace(
        models={
            "regular": LLMModel(
                provider="p", model="regular", max_context_size=1000, capabilities=set()
            ),
            "optional": LLMModel(
                provider="p", model="optional", max_context_size=1000, capabilities={"thinking"}
            ),
            "always": LLMModel(
                provider="p",
                model="always",
                max_context_size=1000,
                capabilities={"thinking", "always_thinking"},
            ),
        }
    )
    for model, values in [("regular", []), ("optional", ["off", "on"]), ("always", ["on"])]:
        options = config_options(config, model, False, SessionState())
        thinking = next((option.root for option in options if option.root.id == "thinking"), None)
        assert ([value.value for value in thinking.options] if thinking else []) == values
        if model == "always":
            assert thinking.current_value == "on"


@pytest.mark.asyncio
async def test_selected_model_and_thinking_survive_restart(tmp_path, monkeypatch):
    import coderai.acp.server as module
    from coderai.config import LLMModel

    original = module._build_engine
    config = SimpleNamespace(
        default_model="regular",
        default_thinking=False,
        models={
            "regular": LLMModel(
                provider="p", model="regular", max_context_size=1000, capabilities=set()
            ),
            "optional": LLMModel(
                provider="p", model="optional", max_context_size=1000, capabilities={"thinking"}
            ),
        },
        providers={"p": SimpleNamespace()},
    )

    def build(session, mcp_configs=None):
        engine = original(session, mcp_configs)
        engine._config = config
        return engine

    monkeypatch.setattr(module, "_build_engine", build)
    server = ACPServer()
    server.conn = SimpleNamespace(session_update=AsyncMock())
    server.client_capabilities = acp.schema.ClientCapabilities()
    server._check_auth = lambda: None
    created = await server.new_session(str(tmp_path))
    await server.set_config_option("model", created.session_id, "optional")
    options = await server.set_config_option("thinking", created.session_id, "on")
    assert [option.root.id for option in options.config_options] == ["model", "thinking", "mode"]
    server.sessions.pop(created.session_id)
    resumed = await server.resume_session(str(tmp_path), created.session_id)
    assert resumed.models.current_model_id == "optional,thinking"
    engine = server.sessions[created.session_id][0].cli
    assert engine.manager.get_active_model() == "optional"
    assert engine.manager.get_thinking_enabled() is True
    await server.set_config_option("model", created.session_id, "regular")
    with pytest.raises(acp.RequestError):
        await server.set_config_option("thinking", created.session_id, "on")
    loaded = await server.load_session(str(tmp_path), created.session_id)
    assert loaded.config_options[-1].root.id == "mode"


@pytest.mark.asyncio
async def test_acp_connection_shutdown_disposes_each_engine_once():
    server = ACPServer()
    engines = [SimpleNamespace(close=AsyncMock()) for _ in range(2)]
    sessions = [SimpleNamespace(cancel=AsyncMock(), cli=engine) for engine in engines]
    server.sessions = {str(index): (session, None) for index, session in enumerate(sessions)}
    server._terminal_bridges["owned"] = object()
    await server.close()
    await server.close()
    for session, engine in zip(sessions, engines):
        session.cancel.assert_awaited_once()
        engine.close.assert_awaited_once()
    assert server.sessions == {}
    assert server._terminal_bridges == {}
    assert server.conn is None
