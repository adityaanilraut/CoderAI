"""Comprehensive test suite for Architecture and Engine Parity in CoderAI."""

import pathlib
import pytest

from coderai.cli.app import _build_parser
from coderai.cli.exec_runner import run_exec_session
from coderai.cli.statusline import (
    StatuslineEngine,
    compute_token_gauge,
    format_default_status_bar,
    strip_ansi,
)
from coderai.core.common.shell_utils import build_shell_env
from coderai.core.common.validate import clean_json_string
from coderai.core.permissions import parse_tool_arguments
from coderai.core.session import MAX_ITERATIONS, SessionManager
from coderai.core.tools.executor import ToolExecutor


def _resp(content: str = "ok", tool_calls=None, usage=None):
    choice = {"message": {"content": content, "tool_calls": tool_calls, "refusal": None}}
    return {
        "choices": [choice],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


# ============================================================================
# 1. Execution Engine Bounds & Constants
# ============================================================================


def test_session_manager_max_iterations_bound(tmp_path: pathlib.Path):
    assert MAX_ITERATIONS == 80_000

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {},
    )
    assert mgr.max_iterations == 80_000

    mgr_custom = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {},
        max_iterations=100,
    )
    assert mgr_custom.max_iterations == 100


# ============================================================================
# 2. Shell Environment & PAGER / NO_COLOR Safeguards
# ============================================================================


def test_shell_env_safeguards():
    env = build_shell_env("/bin/bash")
    assert env["PAGER"] == "cat"
    assert env["NO_COLOR"] == "1"
    assert env["GIT_EDITOR"] == "true"
    assert env["SHELL"] == "/bin/bash"


# ============================================================================
# 3. Tool Validation & JSON Markdown Fence Resiliency
# ============================================================================


def test_clean_json_string_fences():
    # Regular clean json
    assert clean_json_string('{"key": "value"}') == '{"key": "value"}'

    # Markdown json fence
    raw_fence = '```json\n{"key": "value"}\n```'
    assert clean_json_string(raw_fence) == '{"key": "value"}'

    # Generic code fence
    raw_generic = '```\n{"key": "value"}\n```'
    assert clean_json_string(raw_generic) == '{"key": "value"}'

    # Trailing fence with whitespace
    raw_trailing = '{"key": "value"}```'
    assert clean_json_string(raw_trailing) == '{"key": "value"}'


def test_parse_tool_arguments_with_markdown_fences():
    raw_fenced = '```json\n{"file_path": "/tmp/test.txt", "content": "hello"}\n```'
    parsed = parse_tool_arguments(raw_fenced)
    assert parsed == {"file_path": "/tmp/test.txt", "content": "hello"}


def test_tool_executor_argument_resiliency(tmp_path: pathlib.Path):
    executor = ToolExecutor(project_root=str(tmp_path))
    raw_fenced = '```json\n{"file_path": "/tmp/test.txt"}\n```'
    res = executor._parse_tool_arguments(raw_fenced)
    assert res["ok"] is True
    assert res["args"] == {"file_path": "/tmp/test.txt"}


# ============================================================================
# 4. Pluggable Statusline Engine
# ============================================================================


def test_strip_ansi():
    ansi_text = "\x1b[31mRed Text\x1b[0m and \x1b[1;32mGreen\x1b[0m"
    assert strip_ansi(ansi_text) == "Red Text and Green"


def test_statusline_token_gauge():
    disp, style, pct = compute_token_gauge(1000, "gpt-4o")
    assert "1,000" in disp
    assert style in ("green", "yellow", "bold red")
    assert pct >= 0.0


def test_statusline_default_format():
    bar = format_default_status_bar(
        model="gpt-5.6-luna",
        active_tokens=2500,
        plan_mode=True,
        branch="main",
        turns=3,
        mcp_count=2,
    )
    bar_str = str(bar)
    assert "gpt-5.6-luna" in bar_str
    assert "Plan" in bar_str
    assert "main" in bar_str


def test_statusline_command_provider(tmp_path: pathlib.Path):
    settings = {
        "statusline": {
            "type": "command",
            "command": "echo 'Custom Status 123'",
            "ttl": 2.0,
        }
    }
    engine = StatuslineEngine(settings)
    out = engine.execute_command_provider("echo 'Custom Status 123'", str(tmp_path))
    assert "Custom Status 123" in out


def test_statusline_module_provider(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    import sys
    import types

    fake_mod = types.ModuleType("custom_statusline_mod")
    fake_mod.render_statusline = lambda ctx: f"Model is {ctx['model']}"
    monkeypatch.setitem(sys.modules, "custom_statusline_mod", fake_mod)

    settings = {
        "statusline": {
            "type": "module",
            "module": "custom_statusline_mod:render_statusline",
            "ttl": 2.0,
        }
    }
    engine = StatuslineEngine(settings)
    out = engine.execute_module_provider(
        "custom_statusline_mod:render_statusline",
        {"model": "gpt-test-model"},
    )
    assert out == "Model is gpt-test-model"


# ============================================================================
# 5. Headless Exec Runner & CLI Parser
# ============================================================================


def test_cli_parser_exec_flags():
    parser = _build_parser()
    args = parser.parse_args(["--exec", "do something", "--yes", "--plan"])
    assert args.exec_prompt == "do something"
    assert args.yes is True
    assert args.plan is True


@pytest.mark.asyncio
async def test_exec_runner_empty_prompt():
    res = await run_exec_session("")
    assert res == 1


@pytest.mark.asyncio
async def test_exec_runner_single_turn_success(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    class Completions:
        def create(self, **kwargs):
            return _resp("Executed successfully.")

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    monkeypatch.setattr(
        "coderai.cli.exec_runner._core_client",
        lambda: {"client": Client(), "model": "gpt-4o", "thinkingEnabled": False},
    )

    exit_code = await run_exec_session(
        "Say hello",
        project_root=str(tmp_path),
        auto_approve=True,
    )
    assert exit_code == 0
