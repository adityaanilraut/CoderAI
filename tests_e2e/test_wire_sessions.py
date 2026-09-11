"""Session lifecycle tests over the wire protocol.

Covers explicit ``--session`` runs, ``--continue`` chaining, ``/clear`` and
``/compact`` handling, and ``replay`` history streaming. Assertions target
live wire behavior: terminal turn statuses plus re-streamed event payloads.
"""

from __future__ import annotations

from typing import Any

from tests_e2e.wire_helpers import (
    build_shell_tool_call,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _turn_begin_inputs(messages: list[dict[str, Any]]) -> list[Any]:
    return [
        entry["payload"].get("user_input")
        for entry in summarize_messages(messages)
        if entry.get("type") == "TurnBegin"
    ]


def test_session_explicit_id(tmp_path) -> None:
    """An explicit ``--session`` id runs turns to completion."""
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--session", "e2e-session"],
        yolo=True,
    )
    try:
        send_initialize(wire)
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
    finally:
        wire.close()


def test_continue_session_appends(tmp_path) -> None:
    """``--continue`` resumes the previous session for the work directory."""
    config_path = write_scripted_config(tmp_path, ["text: first", "text: second"])
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
                "params": {"user_input": "first"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--continue"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "second"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert inputs and inputs[0].endswith("second")
    finally:
        wire.close()


def test_clear_context(tmp_path) -> None:
    """``/clear`` is accepted and the session keeps serving prompts."""
    config_path = write_scripted_config(tmp_path, ["text: hello", "text: fresh", "text: after"])
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
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "/clear"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert any(text.endswith("/clear") for text in inputs)

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-3",
                "method": "prompt",
                "params": {"user_input": "after clear"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-3")
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert any(text.endswith("after clear") for text in inputs)
    finally:
        wire.close()


def test_manual_compact(tmp_path) -> None:
    """``/compact`` compacts context and the turn finishes."""
    scripts = [
        "text: hello",
        "text: compacted summary",
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
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "/compact"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert any(text.endswith("/compact") for text in inputs)
    finally:
        wire.close()


def test_replay_streams_wire_history(tmp_path) -> None:
    """``replay`` re-streams the recorded wire history for the session."""
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
        extra_args=["--session", "replay-session"],
        yolo=True,
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
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json({"jsonrpc": "2.0", "id": "replay-1", "method": "replay"})
        resp, messages = collect_until_response(wire, "replay-1")
        result = resp.get("result", {})
        assert result.get("status") == "finished"
        assert isinstance(result.get("events"), int) and result["events"] > 0
        assert result.get("requests") == 0
        replayed = summarize_messages(messages)
        assert replayed, "expected replayed wire history"
        turn_begins = [m for m in replayed if m.get("type") == "TurnBegin"]
        assert turn_begins
        assert turn_begins[0]["payload"]["user_input"].endswith("run shell")
    finally:
        wire.close()
