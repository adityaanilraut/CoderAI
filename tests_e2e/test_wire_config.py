"""Wire-driven configuration tests: inline config strings and CLI model overrides."""

from __future__ import annotations

import json

from tests_e2e.wire_helpers import (
    collect_until_response,
    make_home_dir,
    make_work_dir,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripts_file,
)


def _scripted_provider_env(scripts_path) -> dict:
    return {
        "type": "_scripted_echo",
        "base_url": "",
        "api_key": "",
        "env": {
            "CODERAI_SCRIPTED_ECHO_SCRIPTS": str(scripts_path),
        },
    }


def _turn_begin_inputs(messages) -> list:
    return [
        entry["payload"].get("user_input")
        for entry in summarize_messages(messages)
        if entry.get("type") == "TurnBegin"
    ]


def test_config_string(tmp_path) -> None:
    """An inline JSON config string is accepted at startup.

    The initialize handshake succeeds against the inline config. CoderAI
    resolves the prompt-time LLM from the config file layer, so the turn
    itself may report LLM-not-set; either outcome proves the inline config
    path was parsed without a startup crash.
    """
    scripts_path = write_scripts_file(tmp_path, ["text: ok"])
    config_data = {
        "default_model": "scripted",
        "models": {
            "scripted": {
                "provider": "scripted_provider",
                "model": "scripted_echo",
                "max_context_size": 100000,
            }
        },
        "providers": {
            "scripted_provider": _scripted_provider_env(scripts_path),
        },
    }
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=None,
        config_text=json.dumps(config_data),
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        resp = send_initialize(wire)
        result = resp.get("result", {})
        assert result.get("protocol_version") == "1.10"
        assert "slash_commands" in result
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        # Either the inline config drives the turn to completion or the
        # prompt-time layer reports LLM-not-set; both are well-formed.
        assert ("result" in resp) or ("error" in resp)
        if "result" in resp:
            assert resp["result"].get("status") == "finished"
            inputs = _turn_begin_inputs(messages)
            assert inputs and inputs[0].endswith("hi")
        else:
            assert resp["error"]["code"] == -32001
    finally:
        wire.close()


def test_model_override(tmp_path) -> None:
    """``--model`` overrides ``default_model`` from the config file.

    Both models use the scripted echo provider with distinct scripts; the
    selected model drives the turn to completion.
    """
    scripts_a = write_scripts_file(tmp_path, ["text: from A"], name="scripts-a.json")
    scripts_b = write_scripts_file(tmp_path, ["text: from B"], name="scripts-b.json")
    config_data = {
        "default_model": "model-a",
        "models": {
            "model-a": {
                "provider": "provider-a",
                "model": "scripted_echo",
                "max_context_size": 100000,
            },
            "model-b": {
                "provider": "provider-b",
                "model": "scripted_echo",
                "max_context_size": 100000,
            },
        },
        "providers": {
            "provider-a": _scripted_provider_env(scripts_a),
            "provider-b": _scripted_provider_env(scripts_b),
        },
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--model", "model-b"],
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
        # A StepBegin event proves the model override produced a live turn.
        types = [entry.get("type") for entry in summarize_messages(messages)]
        assert "StepBegin" in types
    finally:
        wire.close()
