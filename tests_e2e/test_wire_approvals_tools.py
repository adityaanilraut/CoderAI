"""Tool approval boundary tests over the wire protocol.

CoderAI emits ``ApprovalRequest`` for shell execution; the client answers
with ``approve`` / ``reject`` / ``approve_for_session`` and the turn runs to
a terminal ``finished`` status. Assertions target the live event shapes
(``tool_call_id`` / ``sender`` linkage) rather than Kimi's display blocks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from tests_e2e.wire_helpers import (
    build_approval_response,
    build_shell_tool_call,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _extract_request_payloads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("method") != "request":
            continue
        params = msg.get("params")
        if not isinstance(params, dict):
            continue
        payload = params.get("payload")
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _approval_requests(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        msg
        for msg in summarize_messages(messages)
        if msg.get("type") == "ApprovalRequest"
    ]


def _tool_call_line(tool_call_id: str, name: str, args: Mapping[str, Any]) -> str:
    payload = {"id": tool_call_id, "name": name, "arguments": json.dumps(args)}
    return f"tool_call: {json.dumps(payload)}"


def _turn_begin_inputs(messages: list[dict[str, Any]]) -> list[Any]:
    return [
        entry["payload"].get("user_input")
        for entry in summarize_messages(messages)
        if entry.get("type") == "TurnBegin"
    ]


def test_shell_approval_approve(tmp_path) -> None:
    """Approving a Shell call lets the turn finish; request links the tool call."""
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "echo ok"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp, messages = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve"),
        )
        assert resp.get("result", {}).get("status") == "finished"
        requests = _approval_requests(messages)
        assert len(requests) == 1
        payload = requests[0]["payload"]
        assert payload["tool_call_id"] == "tc-1"
        assert payload["sender"] == "Shell"
        assert payload["id"], "approval request must carry an id"
        inputs = _turn_begin_inputs(messages)
        assert inputs and inputs[0].endswith("run shell")
    finally:
        wire.close()


def test_shell_approval_reject(tmp_path) -> None:
    """Rejecting a Shell call still ends the turn cleanly."""
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "echo ok"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp, messages = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "reject"),
        )
        assert resp.get("result", {}).get("status") == "finished"
        requests = _approval_requests(messages)
        assert len(requests) == 1
        assert requests[0]["payload"]["tool_call_id"] == "tc-1"
    finally:
        wire.close()


def test_approve_for_session(tmp_path) -> None:
    """``approve_for_session`` is accepted; each turn still reports approval."""
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "echo first"),
            ]
        ),
        "text: done",
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-2", "echo second"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp1, messages1 = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve_for_session"),
        )
        assert resp1.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "run shell again"},
            }
        )
        resp2, messages2 = collect_until_response(
            wire,
            "prompt-2",
            request_handler=lambda msg: build_approval_response(msg, "approve"),
        )
        assert resp2.get("result", {}).get("status") == "finished"
        first_ids = [p["tool_call_id"] for p in _extract_request_payloads(messages1)]
        second_ids = [p["tool_call_id"] for p in _extract_request_payloads(messages2)]
        assert first_ids == ["tc-1"]
        assert second_ids == ["tc-2"]
    finally:
        wire.close()


def test_yolo_skips_approval(tmp_path) -> None:
    """``--yolo`` runs non-shell tools without emitting approval requests."""
    scripts = [
        "\n".join(
            [
                "text: start",
                _tool_call_line("tc-1", "SetTodoList", {"todos": []}),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
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
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run yolo"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert all(msg.get("method") != "request" for msg in messages)
        inputs = _turn_begin_inputs(messages)
        assert inputs and inputs[0].endswith("run yolo")
    finally:
        wire.close()


def test_unknown_tool_tolerated(tmp_path) -> None:
    """An unknown scripted tool name does not break the turn."""
    scripts = [
        "\n".join(
            [
                "text: unknown",
                _tool_call_line("tc-1", "NoSuchTool", {}),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
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
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "unknown"},
            }
        )
        resp, _messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()


def test_default_agent_tools_available(tmp_path) -> None:
    """The default agent exposes its bundled tools without approval blocks."""
    scripts = [
        "\n".join(
            [
                "text: agent turn",
                _tool_call_line("tc-1", "SetTodoList", {"todos": []}),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
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
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "agent tools"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        types = [entry.get("type") for entry in summarize_messages(messages)]
        assert "StepBegin" in types
    finally:
        wire.close()
