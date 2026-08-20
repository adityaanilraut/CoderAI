"""Unit and integration tests for recent deepcode parity enhancements.

Tests:
1. Dynamic skill tool execution in ToolRegistry & ToolExecutor
2. Dynamic skill tool execution via ToolExecutor
3. /init command prompt generation and AGENTS.md detection
4. supports_multimodal mode flag ("on", "off", "default")
5. /raw and /init slash commands in completer
6. TOOL_DOCS includes skill and WebFetch
7. reasoningEffort resolved from settings.json and CODERAI_REASONING_EFFORT
8. MCP URL/SSE servers survive settings merge
9. thinking request options honor off/low/max
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import pytest

from coderai.core.common.model_capabilities import supports_multimodal
from coderai.core.prompt import (
    get_effective_project_agents_md_file,
    get_init_command_prompt,
    get_tools,
)
from coderai.core.tools.registry import get_tool_registry
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.types import ToolExecutionHooks, ToolResult
from coderai.cli.completer import AVAILABLE_SLASH_COMMANDS


def test_tool_registry_has_skill_tool() -> None:
    registry = get_tool_registry()
    skill_def = registry.get("skill")
    assert skill_def is not None
    assert skill_def.name == "skill"
    assert "name" in skill_def.required


def test_get_tools_includes_skill() -> None:
    tools = get_tools({"model": "gpt-5.6-sol"})
    names = [t["function"]["name"] for t in tools if "function" in t]
    assert "skill" in names
    assert "Task" in names
    assert "WebSearch" in names


@pytest.mark.asyncio
async def test_tool_executor_skill_execution() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        loaded_skills: list[str] = []

        def mock_load_skill(name: str) -> ToolResult:
            loaded_skills.append(name)
            return ToolResult(ok=True, name="skill", output=f"Loaded {name}")

        executor = ToolExecutor(tmpdir, lambda: {})
        hooks = ToolExecutionHooks(on_load_skill=mock_load_skill)

        result = await executor.execute_tool_call(
            "session-123",
            {
                "id": "call-1",
                "function": {
                    "name": "skill",
                    "arguments": '{"name": "image-generator"}',
                },
            },
            hooks=hooks,
        )

        assert result.ok is True
        assert result.name == "skill"
        assert "Loaded image-generator" in (result.output or "")
        assert loaded_skills == ["image-generator"]


def test_init_command_prompt_without_agents_md() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        prompt = get_init_command_prompt(tmpdir)
        assert "Generate a file named ./AGENTS.md" in prompt
        assert "Repository Guidelines" in prompt
        assert "Recommended Sections" in prompt


def test_init_command_prompt_with_existing_agents_md() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        agents_file = os.path.join(tmpdir, "AGENTS.md")
        with open(agents_file, "w", encoding="utf-8") as f:
            f.write("# Existing Project Guidelines\nSome content.")

        detected = get_effective_project_agents_md_file(tmpdir)
        assert detected == "./AGENTS.md"

        prompt = get_init_command_prompt(tmpdir)
        assert "Update ./AGENTS.md to align it" in prompt
        assert "Repository Guidelines" in prompt


def test_supports_multimodal_mode_override() -> None:
    # deepseek-v4-pro is by default non-multimodal
    assert supports_multimodal("deepseek-v4-pro", "default") is False
    assert supports_multimodal("deepseek-v4-pro", "on") is True
    assert supports_multimodal("deepseek-v4-pro", "off") is False

    # gpt-4o is by default multimodal
    assert supports_multimodal("gpt-4o", "default") is True
    assert supports_multimodal("gpt-4o", "on") is True
    assert supports_multimodal("gpt-4o", "off") is False


def test_slash_commands_include_init_and_raw() -> None:
    cmds = [cmd for cmd, _ in AVAILABLE_SLASH_COMMANDS]
    assert "/init" in cmds
    assert "/raw" in cmds
    assert "/thinking" in cmds
    assert "/plan" in cmds
    assert "/undo" in cmds


def test_tool_docs_include_skill_and_webfetch() -> None:
    from coderai.core.prompt import TOOL_DOCS, get_system_prompt

    assert "## skill" in TOOL_DOCS
    assert "## WebFetch" in TOOL_DOCS
    prompt = get_system_prompt()
    assert "## skill" in prompt
    assert "## WebFetch" in prompt
    assert "## AskUserQuestion" in prompt
    non_interactive = get_system_prompt({"nonInteractive": True})
    assert "## AskUserQuestion" not in non_interactive
    assert "## skill" in non_interactive
    assert "## WebFetch" in non_interactive


def test_reasoning_effort_from_settings_and_env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coderai.core.settings import parse_reasoning_effort, resolve_current_settings

    assert parse_reasoning_effort("off") == "off"
    assert parse_reasoning_effort("none") == "off"
    assert parse_reasoning_effort("LOW") == "low"
    assert parse_reasoning_effort("medium") == "medium"
    assert parse_reasoning_effort("nope") is None

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODERAI_REASONING_EFFORT", raising=False)
    (home / ".coderai").mkdir()
    (home / ".coderai" / "settings.json").write_text(
        json.dumps({"reasoningEffort": "low"}), encoding="utf-8"
    )
    project = tmp_path / "proj"
    (project / ".coderai").mkdir(parents=True)
    (project / ".coderai" / "settings.json").write_text(
        json.dumps({"reasoningEffort": "high"}), encoding="utf-8"
    )

    settings = resolve_current_settings(str(project))
    assert settings["reasoningEffort"] == "high"

    monkeypatch.setenv("CODERAI_REASONING_EFFORT", "off")
    settings = resolve_current_settings(str(project))
    assert settings["reasoningEffort"] == "off"


def test_mcp_url_servers_survive_settings_merge(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coderai.core.settings import resolve_current_settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".coderai").mkdir()
    (home / ".coderai" / "settings.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.com/sse",
                        "headers": {"Authorization": "Bearer user"},
                        "disabled": False,
                    },
                    "echo": {"command": "echo", "env": {"USER_KEY": "u"}},
                }
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "proj"
    (project / ".coderai").mkdir(parents=True)
    (project / ".coderai" / "settings.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "headers": {"X-Project": "1"},
                    },
                    "echo": {"command": "echo", "env": {"PROJ_KEY": "p"}, "cwd": "/tmp"},
                }
            }
        ),
        encoding="utf-8",
    )

    settings = resolve_current_settings(str(project))
    remote = settings["mcpServers"]["remote"]
    assert remote["url"] == "https://mcp.example.com/sse"
    assert "command" not in remote
    assert remote["headers"]["Authorization"] == "Bearer user"
    assert remote["headers"]["X-Project"] == "1"
    assert remote["disabled"] is False
    echo = settings["mcpServers"]["echo"]
    assert echo["command"] == "echo"
    assert echo["env"]["USER_KEY"] == "u"
    assert echo["env"]["PROJ_KEY"] == "p"
    assert echo["cwd"] == "/tmp"


def test_thinking_options_honor_reasoning_effort() -> None:
    from coderai.core.common.openai_thinking import build_thinking_request_options

    assert build_thinking_request_options(
        True, reasoning_effort="off", model="gpt-5.6-sol", has_tools=True
    ) == {"reasoning_effort": "none"}
    assert build_thinking_request_options(
        True, reasoning_effort="max", model="gpt-5.6-sol", has_tools=True
    ) == {"reasoning_effort": "high"}
    assert build_thinking_request_options(
        True, reasoning_effort="low", model="gpt-5.6-sol", has_tools=False
    ) == {"reasoning_effort": "low"}
    assert build_thinking_request_options(
        True, reasoning_effort="max", model="deepseek-v4-pro"
    ) == {"extra_body": {"reasoning_effort": "max"}}
    assert (
        build_thinking_request_options(True, reasoning_effort="off", model="deepseek-v4-pro") == {}
    )
