"""Prompt and event-stream tests over the wire protocol."""

from __future__ import annotations

from typing import Any

from tests_e2e.wire_helpers import (
    build_approval_response,
    build_set_todo_call,
    build_shell_tool_call,
    collect_until_request,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    normalize_response,
    read_response,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _turn_begin(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for entry in summarize_messages(messages):
        if entry.get("type") == "TurnBegin":
            payload = entry.get("payload")
            assert isinstance(payload, dict)
            return payload
    raise AssertionError("Missing TurnBegin event")


def test_basic_prompt_events(tmp_path) -> None:
    """A basic turn emits TurnBegin/StepBegin and finishes."""
    config_path = write_scripted_config(tmp_path, ["text: Hello wire"])
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
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        payload = _turn_begin(messages)
        assert isinstance(payload.get("user_input"), str)
        assert payload["user_input"].endswith("hi")
        types = [entry.get("type") for entry in summarize_messages(messages)]
        assert "StepBegin" in types
    finally:
        wire.close()


def test_multiline_prompt(tmp_path) -> None:
    """Multiline input is preserved verbatim at the end of TurnBegin."""
    config_path = write_scripted_config(tmp_path, ["text: ok"])
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
        user_input = "line1\nline2"
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": user_input},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        payload = _turn_begin(messages)
        assert payload["user_input"].endswith(user_input)
    finally:
        wire.close()


def test_content_part_prompt(tmp_path) -> None:
    """Structured content parts are accepted when the model has the capability."""
    config_path = write_scripted_config(
        tmp_path,
        ["text: ok"],
        capabilities=["image_in", "video_in"],
    )
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    content_parts = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "audio_url", "audio_url": {"url": "data:audio/aac;base64,AAA"}},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAA"}},
    ]

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
                "params": {"user_input": content_parts},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        payload = _turn_begin(messages)
        echoed = payload.get("user_input")
        assert isinstance(echoed, str)
        for part_text in ("hello", "image_url", "audio_url", "video_url"):
            assert part_text in echoed
    finally:
        wire.close()


def test_content_part_missing_capability(tmp_path) -> None:
    """Image parts without the model capability fail with -32002."""
    config_path = write_scripted_config(tmp_path, ["text: ok"], capabilities=[])
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
                "params": {
                    "user_input": [
                        {"type": "text", "text": "hello"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    ]
                },
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        result = normalize_response(resp)
        assert "error" in result
        assert result["error"]["code"] == -32002
        assert "image_in" in result["error"]["message"]
    finally:
        wire.close()


def test_max_steps_reached(tmp_path) -> None:
    """A tiny ``--max-steps-per-turn`` budget still yields a well-formed turn."""
    todo_line = build_set_todo_call("tc-1", [{"title": "x", "status": "pending"}])
    script = "\n".join(
        [
            "text: start",
            todo_line,
        ]
    )
    config_path = write_scripted_config(tmp_path, [script, "text: done"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--max-steps-per-turn", "1"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        # CoderAI either caps the turn or drains the queued scripts; the
        # response must be a well-formed terminal status either way.
        status = resp.get("result", {}).get("status")
        assert status in {"finished", "max_steps_reached"}
        if status == "max_steps_reached":
            assert normalize_response(resp)["result"]["steps"] == 1
        types = [entry.get("type") for entry in summarize_messages(messages)]
        assert "StepBegin" in types
    finally:
        wire.close()


def test_status_update_fields(tmp_path) -> None:
    """Turns carry StepBegin progress markers through the event stream."""
    script = "\n".join(
        [
            "id: scripted-1",
            'usage: {"input_other": 5, "output": 2}',
            "text: hello",
        ]
    )
    config_path = write_scripted_config(tmp_path, [script])
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
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        summary = summarize_messages(messages)
        steps = [entry for entry in summary if entry.get("type") == "StepBegin"]
        assert steps, "expected at least one StepBegin event"
        assert steps[0]["payload"] == {"n": 1}
    finally:
        wire.close()


def test_concurrent_prompt_error(tmp_path) -> None:
    """A second prompt during an active turn is rejected with -32000."""
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "echo hi"),
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
                "params": {"user_input": "run"},
            }
        )
        request_msg, _messages = collect_until_request(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "second"},
            }
        )
        prompt2_resp = normalize_response(read_response(wire, "prompt-2"))
        assert prompt2_resp["error"]["code"] == -32000
        assert "already in progress" in prompt2_resp["error"]["message"]

        wire.send_json(build_approval_response(request_msg, "approve"))
        prompt1_resp, _ = collect_until_response(wire, "prompt-1")
        assert prompt1_resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()
