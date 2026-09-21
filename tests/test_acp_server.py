"""Unit tests for Phase 3: ACP Server and Kaos Integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import acp
import pytest
from kaos.path import KaosPath

from coderai.acp.kaos import ACPKaos
from coderai.acp.server import ACPServer, _ModelIDConv, _expand_llm_models
from coderai.acp.session import (
    ACPSession,
    AcpRunConfig,
    AcpSubagentRunner,
    load_engine_session_id,
    save_engine_session_id,
)
from coderai.config import LLMModel
from coderai.acp.runner import AcpRunConfig as CoreRunConfig
from coderai.acp.runner import AcpSubagentRunner as CoreSubagentRunner


@dataclass
class _MockReadFileResp:
    content: str


@pytest.mark.asyncio
async def test_acp_kaos_lifecycle(tmp_path: Path):
    mock_client = MagicMock()
    mock_client.read_text_file = AsyncMock(return_value=_MockReadFileResp("mock file content"))
    mock_client.write_text_file = AsyncMock(return_value=None)

    fs_cap = acp.schema.FileSystemCapability(read_text_file=True, write_text_file=True)
    caps = acp.schema.ClientCapabilities(fs=fs_cap, terminal=True)

    acp_kaos = ACPKaos(
        client=mock_client,
        session_id="session-42",
        client_capabilities=caps,
    )

    # 1. Fallback / local routing
    assert acp_kaos.name == "acp"
    cwd = acp_kaos.getcwd()
    assert isinstance(cwd, KaosPath)

    norm = acp_kaos.normpath("foo/bar")
    assert isinstance(norm, KaosPath)

    # 2. Routed read
    text = await acp_kaos.readtext("test.txt")
    assert text == "mock file content"
    mock_client.read_text_file.assert_awaited_once()

    # 3. Routed write
    written_len = await acp_kaos.writetext("test.txt", "new data")
    assert written_len == len("new data")
    mock_client.write_text_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_acp_server_initialize():
    server = ACPServer()
    mock_conn = MagicMock()
    server.on_connect(mock_conn)

    resp = await server.initialize(
        protocol_version=1,
        client_capabilities=acp.schema.ClientCapabilities(terminal=True),
        client_info=acp.schema.Implementation(name="test-client", version="1.0.0"),
    )

    assert resp.agent_info.name == "CoderAI"
    assert resp.agent_capabilities.load_session is True
    assert resp.agent_capabilities.prompt_capabilities.image is True
    assert len(resp.auth_methods) > 0
    assert resp.auth_methods[0].id == "login"


@pytest.mark.asyncio
async def test_acp_server_session_lifecycle(tmp_path: Path):
    server = ACPServer()
    mock_conn = MagicMock()
    mock_conn.session_update = AsyncMock()
    server.on_connect(mock_conn)

    caps = acp.schema.ClientCapabilities(terminal=False)
    await server.initialize(protocol_version=1, client_capabilities=caps)

    # Auth gate is covered by dedicated tests; mock it here past the gate
    # (same convention as test_acp_server_fork_and_ext_methods).
    server._check_auth = MagicMock()

    # 1. Create new session
    new_resp = await server.new_session(cwd=str(tmp_path))
    session_id = new_resp.session_id
    assert session_id in server.sessions

    # 2. List sessions
    list_resp = await server.list_sessions(cwd=str(tmp_path))
    assert len(list_resp.sessions) >= 1
    session_ids = [s.session_id for s in list_resp.sessions]
    assert session_id in session_ids

    # 3. Set mode and resume
    await server.set_session_mode("default", session_id)
    resume_resp = await server.resume_session(cwd=str(tmp_path), session_id=session_id)
    assert resume_resp.modes.current_mode_id == "default"


def test_model_id_conv_and_expansion():
    conv1 = _ModelIDConv.from_acp_model_id("gpt-4o")
    assert conv1.model_key == "gpt-4o"
    assert conv1.thinking is False
    assert conv1.to_acp_model_id() == "gpt-4o"

    conv2 = _ModelIDConv.from_acp_model_id("claude-3-7-sonnet,thinking")
    assert conv2.model_key == "claude-3-7-sonnet"
    assert conv2.thinking is True
    assert conv2.to_acp_model_id() == "claude-3-7-sonnet,thinking"

    models = {
        "m1": LLMModel(provider="p1", model="test-thinking-model", max_context_size=128000),
        "m2": LLMModel(provider="p1", model="test-regular-model", max_context_size=128000),
    }
    expanded = _expand_llm_models(models)
    assert len(expanded) >= 2
    model_ids = [m.model_id for m in expanded]
    assert "m1,thinking" in model_ids
    assert "m2" in model_ids


@pytest.mark.asyncio
async def test_acp_session_prompt_and_cancel():
    mock_cli = MagicMock()

    async def mock_run(user_input, cancel_event):
        from coderai.wire.types import TextPart, TurnBegin, TurnEnd

        yield TurnBegin(user_input=user_input)
        yield TextPart(text="Hello from CoderAI")
        yield TurnEnd()

    mock_cli.run = mock_run
    mock_cli.soul.runtime.llm = None

    mock_conn = MagicMock()
    mock_conn.session_update = AsyncMock()

    session = ACPSession(
        id="test-session-prompt",
        cli=mock_cli,
        acp_conn=mock_conn,
        kaos=None,
    )

    prompt_blocks = [acp.schema.TextContentBlock(type="text", text="Say hi")]
    prompt_resp = await session.prompt(prompt_blocks)
    assert prompt_resp.stop_reason == "end_turn"
    mock_conn.session_update.assert_awaited()

    # Test cancel
    assert session._turn_state is None
    await session.cancel()  # No-op when not running


def test_acp_subagent_runner_backward_compatibility():
    # Verify core vs acp re-exports
    assert AcpSubagentRunner is CoreSubagentRunner
    assert AcpRunConfig is CoreRunConfig

    config = AcpRunConfig(command="echo", args=["hi"])
    runner = AcpSubagentRunner(config)
    assert runner.config.command == "echo"
    assert runner._parser is not None


@pytest.mark.asyncio
async def test_acp_server_fork_and_ext_methods(tmp_path: Path):
    server = ACPServer()
    mock_conn = MagicMock()
    mock_conn.session_update = AsyncMock()
    server.on_connect(mock_conn)
    caps = acp.schema.ClientCapabilities(terminal=False)
    await server.initialize(protocol_version=1, client_capabilities=caps)

    # Mock auth check
    server._check_auth = MagicMock()

    # 1. Test ext_method
    ping_resp = await server.ext_method("ping", {})
    assert ping_resp["status"] == "ok"

    ver_resp = await server.ext_method("version", {})
    assert ver_resp["name"] == "CoderAI"

    # ext_notification should not raise
    await server.ext_notification("test_notice", {"foo": "bar"})

    # 2. Test fork_session
    new_resp = await server.new_session(cwd=str(tmp_path))
    orig_session_id = new_resp.session_id

    fork_resp = await server.fork_session(cwd=str(tmp_path), session_id=orig_session_id)
    assert fork_resp.session_id in server.sessions
    assert fork_resp.session_id != orig_session_id
    assert fork_resp.modes.current_mode_id == "default"


def test_engine_session_binding_roundtrip(tmp_path: Path):
    # Missing file -> None (backward compatible with pre-binding sessions).
    assert load_engine_session_id(tmp_path) is None
    assert load_engine_session_id(None) is None

    save_engine_session_id(tmp_path, "engine-abc123")
    assert load_engine_session_id(tmp_path) == "engine-abc123"

    # Corrupt file -> None, never raises.
    (tmp_path / "engine-session-id.json").write_text("{not json", encoding="utf-8")
    assert load_engine_session_id(tmp_path) is None

    # Empty/invalid ids are not persisted.
    save_engine_session_id(tmp_path, "")
    assert load_engine_session_id(tmp_path) is None


@pytest.mark.asyncio
async def test_acp_session_prompt_persists_engine_binding(tmp_path: Path):
    from types import SimpleNamespace

    from coderai.wire.types import TextPart, TurnBegin, TurnEnd

    async def mock_run(user_input, cancel_event):
        yield TurnBegin(user_input=user_input)
        yield TextPart(text="hi")
        yield TurnEnd()

    mock_cli = SimpleNamespace(
        run=mock_run, session_id="engine-abc123", session=SimpleNamespace(dir=tmp_path)
    )
    mock_conn = MagicMock()
    mock_conn.session_update = AsyncMock()

    session = ACPSession(id="acp-1", cli=mock_cli, acp_conn=mock_conn, kaos=None)
    prompt_blocks = [acp.schema.TextContentBlock(type="text", text="Say hi")]
    prompt_resp = await session.prompt(prompt_blocks)
    assert prompt_resp.stop_reason == "end_turn"
    assert load_engine_session_id(tmp_path) == "engine-abc123"


@pytest.mark.asyncio
async def test_setup_session_rehydrates_persisted_binding(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    import coderai.acp.server as server_module

    bound: list[str] = []

    async def fake_find(work_dir, session_id):
        return SimpleNamespace(id="acp-1", dir=tmp_path)

    def fake_build_engine(session, mcp_configs=None):
        manager = SimpleNamespace(
            get_session=lambda sid: object() if sid == "engine-abc123" else None
        )
        return SimpleNamespace(
            manager=manager,
            config=SimpleNamespace(default_model="", default_thinking=False),
            session_id=None,
            bind_session=lambda sid: bound.append(sid),
        )

    monkeypatch.setattr(server_module.Session, "find", staticmethod(fake_find))
    monkeypatch.setattr(server_module, "_build_engine", fake_build_engine)

    save_engine_session_id(tmp_path, "engine-abc123")

    server = ACPServer()
    mock_conn = MagicMock()
    server.on_connect(mock_conn)
    server.client_capabilities = acp.schema.ClientCapabilities(terminal=False)

    acp_session, _ = await server._setup_session(str(tmp_path), "acp-1")
    assert bound == ["engine-abc123"]
    assert "acp-1" in server.sessions
    assert acp_session.id == "acp-1"


@pytest.mark.asyncio
async def test_setup_session_legacy_same_id_fallback(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    import coderai.acp.server as server_module

    bound: list[str] = []

    async def fake_find(work_dir, session_id):
        return SimpleNamespace(id="acp-1", dir=tmp_path)

    def fake_build_engine(session, mcp_configs=None):
        # No persisted binding file; only the legacy same-id entry exists.
        manager = SimpleNamespace(get_session=lambda sid: object() if sid == "acp-1" else None)
        return SimpleNamespace(
            manager=manager,
            config=SimpleNamespace(default_model="", default_thinking=False),
            session_id=None,
            bind_session=lambda sid: bound.append(sid),
        )

    monkeypatch.setattr(server_module.Session, "find", staticmethod(fake_find))
    monkeypatch.setattr(server_module, "_build_engine", fake_build_engine)

    server = ACPServer()
    mock_conn = MagicMock()
    server.on_connect(mock_conn)
    server.client_capabilities = acp.schema.ClientCapabilities(terminal=False)

    await server._setup_session(str(tmp_path), "acp-1")
    assert bound == ["acp-1"]


# -- P4: per-session MCP injection -------------------------------------------


def _stdio_server(name: str, command: str = "npx") -> acp.schema.McpServerStdio:
    return acp.schema.McpServerStdio(name=name, command=command, args=["-y", "x"], env=[])


def test_mcp_configs_to_servers_accepts_config_and_dict():
    from coderai.acp.mcp import acp_mcp_servers_to_mcp_config
    from coderai.acp.server import _mcp_configs_to_servers

    assert _mcp_configs_to_servers(None) == {}
    assert _mcp_configs_to_servers([]) == {}

    cfg = acp_mcp_servers_to_mcp_config([_stdio_server("s1")])
    assert _mcp_configs_to_servers([cfg]) == {
        "s1": {"command": "npx", "args": ["-y", "x"], "env": {}, "transport": "stdio"}
    }
    assert _mcp_configs_to_servers([{"mcpServers": {"s2": {"command": "uvx"}}}]) == {
        "s2": {"command": "uvx"}
    }
    # Malformed entries are skipped, never raised.
    assert _mcp_configs_to_servers([cfg, None, {"mcpServers": None}, 42]) == {
        "s1": {"command": "npx", "args": ["-y", "x"], "env": {}, "transport": "stdio"}
    }


def test_build_engine_is_session_scoped_and_leaves_env_alone(tmp_path: Path, monkeypatch):
    """Concurrent ACP sessions must not collide via process-global env."""
    from types import SimpleNamespace

    import coderai.acp.server as server_module
    from coderai.acp.mcp import acp_mcp_servers_to_mcp_config

    monkeypatch.delenv("CODERAI_MCP_CONFIG_JSON", raising=False)

    first = SimpleNamespace(work_dir=str(tmp_path / "one"))
    second = SimpleNamespace(work_dir=str(tmp_path / "two"))
    engine_one = server_module._build_engine(
        first, mcp_configs=[acp_mcp_servers_to_mcp_config([_stdio_server("only-one")])]
    )
    engine_two = server_module._build_engine(
        second, mcp_configs=[acp_mcp_servers_to_mcp_config([_stdio_server("only-two")])]
    )

    one_servers = engine_one.manager.get_resolved_settings().get("mcpServers") or {}
    two_servers = engine_two.manager.get_resolved_settings().get("mcpServers") or {}
    assert "only-one" in one_servers and "only-two" not in one_servers
    assert "only-two" in two_servers and "only-one" not in two_servers
    assert "CODERAI_MCP_CONFIG_JSON" not in __import__("os").environ


def test_build_session_manager_mcp_overlay_merges_without_env(tmp_path: Path, monkeypatch):
    """The factory overlay must merge over base settings (no env consulted)."""
    import os

    from coderai.cli.session_factory import build_session_manager

    monkeypatch.delenv("CODERAI_MCP_CONFIG_JSON", raising=False)
    manager = build_session_manager(
        str(tmp_path),
        non_interactive=True,
        mcp_servers={"overlay-srv": {"command": "npx", "args": ["-y", "overlay"]}},
    )
    servers = manager.get_resolved_settings().get("mcpServers") or {}
    assert servers.get("overlay-srv") == {"command": "npx", "args": ["-y", "overlay"]}
    assert "CODERAI_MCP_CONFIG_JSON" not in os.environ


# -- P4: session-scoped model switching ---------------------------------------


def _model_server(model_key="m1", thinking=False):
    from types import SimpleNamespace

    from coderai.acp.server import _ModelIDConv

    engine = SimpleNamespace(
        config=SimpleNamespace(
            models={model_key: SimpleNamespace(provider="p1")},
            providers={"p1": SimpleNamespace()},
            default_model=model_key,
            default_thinking=False,
        ),
        applied=[],
        thinking=[],
        set_model=lambda key: engine.applied.append(key),
        set_thinking=lambda flag: engine.thinking.append(flag),
    )
    session = SimpleNamespace(cli=engine)
    server = ACPServer()
    server.sessions["sid-1"] = (session, _ModelIDConv(model_key, thinking))
    return server, engine


@pytest.mark.asyncio
async def test_set_session_model_is_session_scoped():
    """Model switches must override the session only: no global config write."""
    server, engine = _model_server()
    before = (engine.config.default_model, engine.config.default_thinking)

    await server.set_session_model("m1,thinking", "sid-1")

    assert engine.applied == ["m1"]
    assert engine.thinking == [True]
    # In-memory config object untouched (previously rewritten + saved to disk).
    assert (engine.config.default_model, engine.config.default_thinking) == before
    # Advertised session model tracks the switch (previously left stale).
    assert server.sessions["sid-1"][1].thinking is True


@pytest.mark.asyncio
async def test_set_session_model_noop_when_unchanged():
    server, engine = _model_server()
    await server.set_session_model("m1", "sid-1")
    assert engine.applied == []
    assert engine.thinking == []


@pytest.mark.asyncio
async def test_set_session_model_rejects_unknown_model():
    server, engine = _model_server()
    with pytest.raises(acp.RequestError):
        await server.set_session_model("nope", "sid-1")
    assert engine.applied == []


@pytest.mark.asyncio
async def test_set_session_model_rejects_unknown_session():
    server, _ = _model_server()
    with pytest.raises(acp.RequestError):
        await server.set_session_model("m1", "missing")


def test_thinking_override_is_session_scoped(tmp_path: Path):
    """Manager thinking override must shadow resolved settings per session."""
    from coderai.cli.session_factory import build_session_manager

    manager = build_session_manager(str(tmp_path), non_interactive=True)
    baseline = manager.get_thinking_enabled()
    assert isinstance(baseline, bool)
    manager.set_thinking_enabled(not baseline)
    assert manager.get_thinking_enabled() is (not baseline)


def test_engine_set_thinking_tolerates_minimal_managers():
    """Engines bound to fakes without the setter must not crash on switch."""
    from types import SimpleNamespace

    from coderai.acp.engine import SessionManagerEngine

    engine = SessionManagerEngine(SimpleNamespace())
    engine.set_thinking(True)  # must not raise


# -- Group C: auth gate + ext allowlist + fork validation ----------------------


def _authed_server(monkeypatch=None):
    server = ACPServer()
    mock_conn = MagicMock()
    mock_conn.session_update = AsyncMock()
    server.on_connect(mock_conn)
    server.client_capabilities = acp.schema.ClientCapabilities(terminal=False)
    server._check_auth = MagicMock()
    return server


def test_token_fallthrough_requires_auth(monkeypatch):
    """No OAuth token and no API keys must report a reason, not fall through."""
    from types import SimpleNamespace

    import coderai.acp.server as server_module

    def _boom(ref):
        raise OSError("no token file")

    monkeypatch.setattr(server_module, "load_tokens", _boom)
    monkeypatch.setattr(server_module, "load_config", lambda: SimpleNamespace(providers={}))

    server = ACPServer()
    reason = server._check_token_usable()
    assert isinstance(reason, str) and reason


@pytest.mark.asyncio
async def test_new_session_requires_auth_without_credentials(monkeypatch, tmp_path: Path):
    """The fixed fallthrough must surface as AUTH_REQUIRED, not a session."""
    from types import SimpleNamespace

    import coderai.acp.server as server_module

    def _boom(ref):
        raise OSError("no token file")

    monkeypatch.setattr(server_module, "load_tokens", _boom)
    monkeypatch.setattr(server_module, "load_config", lambda: SimpleNamespace(providers={}))

    server = ACPServer()
    mock_conn = MagicMock()
    mock_conn.session_update = AsyncMock()
    server.on_connect(mock_conn)
    server.client_capabilities = acp.schema.ClientCapabilities(terminal=False)

    with pytest.raises(acp.RequestError):
        await server.new_session(cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_ext_method_rejects_unknown():
    """Unlisted ext methods must raise method_not_found, not an error dict."""
    server = _authed_server()

    with pytest.raises(acp.RequestError) as exc_info:
        await server.ext_method("nope/not-real", {})
    assert exc_info.value.code == -32601

    with pytest.raises(acp.RequestError) as exc_info:
        await server.ext_method("terminal/bogus-op", {"session_id": "s"})
    assert exc_info.value.code == -32601


@pytest.mark.asyncio
async def test_fork_unknown_parent_creates_nothing(tmp_path: Path):
    """Fork validation must run before Session.create (no orphan sessions)."""
    server = _authed_server()

    with pytest.raises(acp.RequestError):
        await server.fork_session(cwd=str(tmp_path), session_id="missing-parent")
    assert server.sessions == {}


@pytest.mark.asyncio
async def test_list_sessions_rejects_bad_cursor(tmp_path: Path):
    server = _authed_server()

    with pytest.raises(acp.RequestError):
        await server.list_sessions(cursor="not-an-offset", cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_session_streams_wire_think_and_tool_parts():
    """Wire ThinkPart(text=) and ToolCallPart(arguments=) must reach the client."""
    from coderai.wire.types import (
        QuestionRequest,
        ThinkPart,
        ToolCallPart,
        TurnBegin,
        TurnEnd,
    )
    from kosong.message import ToolCall

    async def mock_run(user_input, cancel_event):
        yield TurnBegin(user_input=user_input)
        yield ThinkPart(text="hmm")
        yield ToolCall(id="c1", function={"name": "bash", "arguments": "{}"})
        yield ToolCallPart(id="c1", name="bash", arguments="--more")
        yield QuestionRequest(id="q1", tool_call_id="", questions=[])
        yield TurnEnd()

    mock_cli = MagicMock()
    mock_cli.run = mock_run
    mock_conn = MagicMock()
    mock_conn.session_update = AsyncMock()

    session = ACPSession(id="s-think", cli=mock_cli, acp_conn=mock_conn, kaos=None)
    resp = await session.prompt([acp.schema.TextContentBlock(type="text", text="hi")])
    assert resp.stop_reason == "end_turn"

    updates = [call.kwargs["update"] for call in mock_conn.session_update.await_args_list]
    assert any(type(update).__name__ == "AgentThoughtChunk" for update in updates), (
        "wire ThinkPart was not forwarded as a thought chunk"
    )
    progress_updates = [update for update in updates if type(update).__name__ == "ToolCallProgress"]
    assert progress_updates, "wire ToolCallPart produced no tool_call_update"


@pytest.mark.asyncio
async def test_runner_close_releases_process():
    """close() must reap the child, pipes, and reader thread."""
    import asyncio

    config = CoreRunConfig(command="echo", args=["hi"])
    runner = CoreSubagentRunner(config)
    runner._start_process()
    await asyncio.sleep(0.2)
    await runner.close()
    assert runner.process is None
    assert runner._reader_thread is None


def test_runner_sends_integer_protocol_version():
    """ACP protocolVersion is a negotiation integer, not a spec-tag string."""
    from coderai.acp.version import CURRENT_VERSION

    assert isinstance(CURRENT_VERSION.protocol_version, int)
