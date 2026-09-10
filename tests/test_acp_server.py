"""Unit tests for Phase 3: ACP Server and Kaos Integration."""

from __future__ import annotations

import asyncio
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
    get_current_acp_tool_call_id_or_none,
)
from coderai.acp.tools import Terminal, replace_tools
from coderai.config import LLMModel
from coderai.core.acp.runner import AcpRunConfig as CoreRunConfig
from coderai.core.acp.runner import AcpSubagentRunner as CoreSubagentRunner
from coderai.soul.agent import Runtime
from coderai.soul.approval import Approval
from coderai.soul.toolset import KimiToolset
from coderai.tools.shell import Params as ShellParams, Shell


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


def test_replace_tools():
    toolset = KimiToolset()
    approval = Approval(yolo=True)
    shell = Shell(approval=approval)
    toolset.add(shell)

    assert toolset.find(Shell) is not None

    mock_client = MagicMock()
    runtime = MagicMock()
    runtime.approval = approval

    caps = acp.schema.ClientCapabilities(terminal=True)
    replace_tools(caps, mock_client, "session-123", toolset, runtime)

    terminal_tool = toolset.find(Terminal)
    assert terminal_tool is not None
    assert terminal_tool.name == "Shell"
    assert isinstance(terminal_tool, Terminal)


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

    # 1. Create new session
    new_resp = await server.new_session(cwd=str(tmp_path))
    session_id = new_resp.session_id
    assert session_id in server.sessions

    # Append a message so session is non-empty on disk
    acp_session, _ = server.sessions[session_id]
    from kosong.message import Message, TextPart
    await acp_session.cli.soul.context.append_message(
        Message(role="user", content=[TextPart(text="Hello CoderAI")])
    )

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
