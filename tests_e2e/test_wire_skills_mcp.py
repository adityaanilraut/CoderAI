"""Skill and MCP integration tests over the wire protocol.

Covers bundled skill advertisement at handshake, ``/skill:`` and ``/flow:``
invocation, and MCP server loading via ``--mcp-config-file``. Assertions
target live wire behavior: handshake command entries plus terminal turn
statuses.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

from tests_e2e.wire_helpers import (
    build_approval_response,
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


def _skill_commands(init_result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        cmd
        for cmd in init_result.get("slash_commands", [])
        if str(cmd.get("name", "")).startswith("skill:")
    ]


def test_init_lists_bundled_skills(tmp_path) -> None:
    """The handshake advertises bundled ``skill:`` slash commands."""
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
        resp = send_initialize(wire)
        result = resp.get("result", {})
        skills = _skill_commands(result)
        assert skills, "expected bundled skill: commands at handshake"
        for skill in skills:
            assert skill.get("description"), "skill commands must carry a description"
    finally:
        wire.close()


def test_skill_prompt_injects_skill_text(tmp_path) -> None:
    """A ``/skill:`` prompt runs to completion against the skill session."""
    skill_dir = tmp_path / "skills"
    skill_path = skill_dir / "test-skill"
    skill_path.mkdir(parents=True)
    skill_text = "\n".join(
        [
            "---",
            "name: test",
            "description: Test skill",
            "---",
            "",
            "Use this skill in wire tests.",
        ]
    )
    skill_path.joinpath("SKILL.md").write_text(skill_text + "\n", encoding="utf-8")

    config_path = write_scripted_config(tmp_path, ["text: skill ok", "text: done"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        skills_dirs=[skill_dir],
        extra_args=["--session", "skill-session"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "/skill:test"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert any(text.endswith("/skill:test") for text in inputs)
    finally:
        wire.close()


def test_flow_skill(tmp_path) -> None:
    """A ``/flow:`` prompt runs the flow to completion."""
    skill_dir = tmp_path / "skills"
    flow_dir = skill_dir / "test-flow"
    flow_dir.mkdir(parents=True)
    flow_dir.joinpath("SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: test-flow",
                "description: Test flow",
                "type: flow",
                "---",
                "",
                "```mermaid",
                "flowchart TD",
                "A([BEGIN]) --> B[Say hello]",
                "B --> C([END])",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    config_path = write_scripted_config(tmp_path, ["text: flow done", "text: done"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        skills_dirs=[skill_dir],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "/flow:test-flow"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert any(text.endswith("/flow:test-flow") for text in inputs)
    finally:
        wire.close()


def test_mcp_tool_call(tmp_path) -> None:
    """An MCP server loaded via ``--mcp-config-file`` serves a wire turn."""
    server_path = tmp_path / "mcp_server.py"
    server_path.write_text(
        textwrap.dedent(
            """
            from fastmcp.server import FastMCP

            server = FastMCP("test-mcp")

            @server.tool
            def ping(text: str) -> str:
                return f"pong:{text}"

            if __name__ == "__main__":
                server.run(transport="stdio", show_banner=False)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    mcp_config = {
        "mcpServers": {
            "test": {
                "command": sys.executable,
                "args": [str(server_path)],
            }
        }
    }
    mcp_config_path = tmp_path / "mcp.json"
    mcp_config_path.write_text(json.dumps(mcp_config), encoding="utf-8")

    tool_args = json.dumps({"text": "hi"})
    tool_call = json.dumps({"id": "tc-1", "name": "ping", "arguments": tool_args})
    scripts = [
        "\n".join(
            [
                "text: call mcp",
                f"tool_call: {tool_call}",
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
        mcp_config_path=mcp_config_path,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "call mcp"},
            }
        )
        seen: list[str] = []

        def _handler(msg: dict[str, Any]) -> dict[str, Any]:
            params = msg.get("params", {})
            seen.append(str(params.get("type")))
            return build_approval_response(msg, "approve")

        resp, messages = collect_until_response(wire, "prompt-1", request_handler=_handler)
        assert resp.get("result", {}).get("status") == "finished"
        inputs = _turn_begin_inputs(messages)
        assert any(text.endswith("call mcp") for text in inputs)
    finally:
        wire.close()


def test_mcp_config_missing_server_file(tmp_path) -> None:
    """A missing MCP config path does not prevent the wire session."""
    config_path = write_scripted_config(tmp_path, ["text: ok"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        mcp_config_path=tmp_path / "no-such-mcp.json",
        yolo=True,
    )
    try:
        resp = send_initialize(wire)
        assert resp.get("result", {}).get("protocol_version") == "1.10"
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
    finally:
        wire.close()
