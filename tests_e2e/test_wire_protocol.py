"""Wire protocol framing tests: handshake, external tools, and init-less prompts."""

from __future__ import annotations

from typing import Any

from tests_e2e.wire_helpers import (
    collect_until_response,
    make_home_dir,
    make_work_dir,
    normalize_response,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _turn_begin_inputs(messages: list[dict[str, Any]]) -> list[Any]:
    return [
        entry["payload"].get("user_input")
        for entry in summarize_messages(messages)
        if entry.get("type") == "TurnBegin"
    ]


def test_initialize_handshake(tmp_path) -> None:
    """``initialize`` returns the protocol version, server identity, commands."""
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        resp = send_initialize(wire)
        result = _as_dict(resp.get("result"))
        assert result.get("protocol_version") == "1.10"
        assert "slash_commands" in result
        assert isinstance(result["slash_commands"], list)
        assert result["slash_commands"], "expected a non-empty slash command catalog"
        for command in result["slash_commands"]:
            assert {"name", "description", "aliases"} <= set(command)
        normalized = normalize_response(resp)
        server = normalized["result"]["server"]
        # The CoderAI server name normalizes to the shared CLI identity.
        assert server == {"name": "Kimi Code CLI", "version": "<VERSION>"}
        hooks = normalized["result"]["hooks"]
        assert "PreToolUse" in hooks["supported_events"]
        assert normalized["result"]["capabilities"] == {"supports_question": True}
    finally:
        wire.close()


def test_initialize_external_tool_conflict(tmp_path) -> None:
    """Registering a builtin name as an external tool is rejected with a reason."""
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    external_tools = [
        {
            "name": "Shell",
            "description": "Conflicts with built-in",
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        resp = send_initialize(wire, external_tools=external_tools)
        result = _as_dict(resp.get("result"))
        external_tools_result = _as_dict(result.get("external_tools"))
        rejected = external_tools_result.get("rejected")
        assert isinstance(rejected, list)
        assert any(isinstance(item, dict) and item.get("name") == "Shell" for item in rejected)
        for item in rejected:
            assert item.get("reason"), "rejection must carry a reason"
    finally:
        wire.close()


def test_external_tool_call(tmp_path) -> None:
    """Client-executed external tools are rejected; the turn still completes."""
    config_path = write_scripted_config(tmp_path, ["text: done"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    external_tools = [
        {
            "name": "ext_tool",
            "description": "External tool",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        resp = send_initialize(wire, external_tools=external_tools)
        result = _as_dict(resp.get("result"))
        rejected = _as_dict(result.get("external_tools")).get("rejected")
        assert isinstance(rejected, list)
        assert any(item.get("name") == "ext_tool" for item in rejected)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run external tool"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert inputs and inputs[0].endswith("run external tool")
    finally:
        wire.close()


def test_prompt_without_initialize(tmp_path) -> None:
    """A prompt sent before ``initialize`` still completes a turn."""
    config_path = write_scripted_config(tmp_path, ["text: hello without init"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert inputs and inputs[0].endswith("hi")
        types = [entry.get("type") for entry in summarize_messages(messages)]
        assert "StepBegin" in types
    finally:
        wire.close()
