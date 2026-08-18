"""Unit tests for Phase 4: IDE Companion & Headless JSON-RPC 2.0 Server Mode Bridge."""

from __future__ import annotations

import json
import pathlib
import pytest

from coderai._version import __version__
from coderai.cli.app import _build_parser
from coderai.core.server.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    PARSE_ERROR,
    SERVER_NOT_INITIALIZED,
    format_error,
    format_notification,
    format_response,
    parse_message,
)
from coderai.core.server.server import CoderAIServer
from coderai.core.session import SessionManager, SessionMessage


def _mock_response(content: str = "ok", tool_calls=None, thinking=None):
    choice = {
        "message": {
            "content": content,
            "tool_calls": tool_calls,
            "reasoning_content": thinking,
            "refusal": None,
        }
    }
    return {
        "choices": [choice],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }


# ============================================================================
# 1. JSON-RPC 2.0 Protocol (protocol.py)
# ============================================================================


def test_protocol_parse_message_valid():
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    ok, parsed, err = parse_message(raw)
    assert ok is True
    assert parsed is not None
    assert err is None
    assert parsed["method"] == "initialize"
    assert parsed["id"] == 1


def test_protocol_parse_message_errors():
    # Empty string
    ok, parsed, err = parse_message("")
    assert not ok and parsed is None and err is None

    # Invalid JSON
    ok, parsed, err = parse_message("{bad_json")
    assert not ok and err is not None
    assert err["error"]["code"] == PARSE_ERROR

    # Non-dict JSON
    ok, parsed, err = parse_message("12345")
    assert not ok and err is not None
    assert err["error"]["code"] == INVALID_REQUEST

    # Missing jsonrpc 2.0
    ok, parsed, err = parse_message(json.dumps({"id": 1, "method": "ping"}))
    assert not ok and err is not None
    assert err["error"]["code"] == INVALID_REQUEST

    # Missing method
    ok, parsed, err = parse_message(json.dumps({"jsonrpc": "2.0", "id": 1}))
    assert not ok and err is not None
    assert err["error"]["code"] == INVALID_REQUEST


def test_protocol_formatting_helpers():
    resp = format_response("req_123", {"status": "ok"})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "req_123"
    assert resp["result"]["status"] == "ok"

    err = format_error("req_123", INVALID_PARAMS, "Invalid params", data={"field": "prompt"})
    assert err["jsonrpc"] == "2.0"
    assert err["error"]["code"] == INVALID_PARAMS
    assert err["error"]["data"]["field"] == "prompt"

    notif = format_notification("turn_start", {"sessionId": "s1", "turnIndex": 1})
    assert notif["jsonrpc"] == "2.0"
    assert "id" not in notif
    assert notif["method"] == "turn_start"
    assert notif["params"]["turnIndex"] == 1


# ============================================================================
# 2. Server Lifecycle & Initialization (server.py)
# ============================================================================


@pytest.mark.asyncio
async def test_server_lifecycle_and_uninitialized_guard(tmp_path: pathlib.Path):
    server = CoderAIServer(project_root=str(tmp_path), model="gpt-4o")

    # Calling method before initialize should return SERVER_NOT_INITIALIZED (-32002)
    res_before = await server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "session/list", "params": {}}
    )
    assert "error" in res_before
    assert res_before["error"]["code"] == SERVER_NOT_INITIALIZED

    # Ping is allowed before initialize
    res_ping = await server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert res_ping["result"] == {"pong": True}

    # Initialize
    res_init = await server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}}
    )
    assert "result" in res_init
    assert res_init["result"]["serverInfo"]["name"] == "coderai-server"
    assert res_init["result"]["serverInfo"]["version"] == __version__
    assert res_init["result"]["capabilities"]["streaming"] is True
    assert server.initialized is True

    # Shutdown
    res_shutdown = await server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "shutdown"})
    assert res_shutdown["result"] == {"status": "ok"}


# ============================================================================
# 3. Session Operations & Event Streaming (server.py)
# ============================================================================


@pytest.mark.asyncio
async def test_server_session_create_prompt_and_events(tmp_path: pathlib.Path):
    emitted_events: list[dict] = []

    class Completions:
        def create(self, **kwargs):
            return _mock_response("Server mode execution completed.")

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": Client(), "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )

    server = CoderAIServer(
        project_root=str(tmp_path),
        model="gpt-4o",
        session_manager=mgr,
        event_sink=lambda evt: emitted_events.append(evt),
    )

    # Initialize server
    await server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    # Create session with initial prompt
    res_create = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/create",
            "params": {"prompt": "Create test project", "planMode": False},
        }
    )
    assert "result" in res_create
    session_id = res_create["result"]["sessionId"]
    assert session_id

    # Verify event notifications were emitted: turn_start and turn_finish
    methods_emitted = [e["method"] for e in emitted_events]
    assert "turn_start" in methods_emitted
    assert "turn_finish" in methods_emitted

    # Send follow-up prompt
    emitted_events.clear()
    res_prompt = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": "Follow up question"},
        }
    )
    assert res_prompt["result"]["sessionId"] == session_id

    # List sessions
    res_list = await server.handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "session/list", "params": {}}
    )
    assert len(res_list["result"]["sessions"]) == 1

    # Get session
    res_get = await server.handle_request(
        {"jsonrpc": "2.0", "id": 5, "method": "session/get", "params": {"sessionId": session_id}}
    )
    assert res_get["result"]["session"]["id"] == session_id
    assert len(res_get["result"]["messages"]) > 0

    # Fork session
    res_fork = await server.handle_request(
        {"jsonrpc": "2.0", "id": 6, "method": "session/fork", "params": {"sessionId": session_id}}
    )
    forked_id = res_fork["result"]["forkedSessionId"]
    assert forked_id and forked_id != session_id

    # Diff
    res_diff = await server.handle_request(
        {"jsonrpc": "2.0", "id": 7, "method": "session/diff", "params": {"sessionId": session_id}}
    )
    assert "diff" in res_diff["result"]

    # Interrupt
    res_intr = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "session/interrupt",
            "params": {"sessionId": session_id},
        }
    )
    assert res_intr["result"]["interrupted"] is True

    # Delete session
    res_del = await server.handle_request(
        {"jsonrpc": "2.0", "id": 9, "method": "session/delete", "params": {"sessionId": session_id}}
    )
    assert res_del["result"]["deleted"] is True


@pytest.mark.asyncio
async def test_server_tool_result_and_chunk_streaming(tmp_path: pathlib.Path):
    emitted_events: list[dict] = []

    server = CoderAIServer(
        project_root=str(tmp_path),
        model="gpt-4o",
        event_sink=lambda evt: emitted_events.append(evt),
    )
    server.active_turn_session_id = "test_sess_1"

    # Simulate assistant message with thinking, tool calls, and tool results
    msg_thinking = SessionMessage(
        id="m_th",
        session_id="test_sess_1",
        role="assistant",
        thinking="Analyzing workspace structure...",
    )
    server._handle_assistant_message(msg_thinking)

    msg_tc = SessionMessage(
        id="m_tc",
        session_id="test_sess_1",
        role="assistant",
        tool_calls=[
            {
                "id": "tc_bash_1",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "ls -la"})},
            }
        ],
    )
    server._handle_assistant_message(msg_tc)

    msg_tr = SessionMessage(
        id="m_tr",
        session_id="test_sess_1",
        role="tool",
        content=json.dumps(
            {
                "name": "bash",
                "ok": True,
                "output": "file1.txt\nfile2.txt",
                "metadata": {"exit_code": 0},
            }
        ),
    )
    server._handle_assistant_message(msg_tr)

    types = [e["method"] for e in emitted_events]
    assert "stream_chunk" in types
    assert "tool_call" in types
    assert "tool_result" in types

    tc_event = next(e for e in emitted_events if e["method"] == "tool_call")
    assert tc_event["params"]["name"] == "bash"
    assert tc_event["params"]["toolCallId"] == "tc_bash_1"

    tr_event = next(e for e in emitted_events if e["method"] == "tool_result")
    assert tr_event["params"]["name"] == "bash"
    assert tr_event["params"]["ok"] is True
    assert "file1.txt" in tr_event["params"]["output"]


# ============================================================================
# 4. Models, MCP & Skills RPC Endpoints (server.py)
# ============================================================================


@pytest.mark.asyncio
async def test_server_models_mcp_and_skills_endpoints(tmp_path: pathlib.Path):
    server = CoderAIServer(project_root=str(tmp_path), model="gpt-4o")
    await server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    # Model list
    res_models = await server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}}
    )
    assert len(res_models["result"]["models"]) > 0
    assert res_models["result"]["activeModel"] == "gpt-4o"

    # Model set & get
    res_set = await server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "model/set", "params": {"model": "claude-3-7-sonnet"}}
    )
    assert res_set["result"]["model"] == "claude-3-7-sonnet"

    res_get = await server.handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "model/get", "params": {}}
    )
    assert res_get["result"]["model"] == "claude-3-7-sonnet"

    # MCP status
    res_mcp = await server.handle_request(
        {"jsonrpc": "2.0", "id": 5, "method": "mcp/status", "params": {}}
    )
    assert "servers" in res_mcp["result"]

    # Skills list
    res_skills = await server.handle_request(
        {"jsonrpc": "2.0", "id": 6, "method": "skills/list", "params": {}}
    )
    assert "skills" in res_skills["result"]


# ============================================================================
# 5. CLI Server Mode Flag Parsing (app.py)
# ============================================================================


def test_cli_server_mode_argument_parsing():
    parser = _build_parser()

    args_flag = parser.parse_args(["--server"])
    assert args_flag.server is True

    args_serve = parser.parse_args(["--serve"])
    assert args_serve.server is True

    args_cmd = parser.parse_args(["serve"])
    assert args_cmd.prompt == ["serve"]
