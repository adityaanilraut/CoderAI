"""Output, deadline, schema, and catalog regression coverage."""

from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path

import pytest

from coderai.tools.file._search_common import _matches_glob, SearchError
from coderai.tools.file.grep import _python_grep, _python_grep_worker
from coderai.tools.legacy.catalog import render_catalog
from coderai.tools.legacy.registry import ToolRegistry
from coderai.utils.bounded_process import bounded_run, OutputLimitError


def test_subprocess_caps_during_collection_and_bounds_stderr():
    with pytest.raises(OutputLimitError):
        bounded_run(
            [sys.executable, "-c", "import sys;sys.stdout.write('x'*1000000)"],
            timeout=3,
            stdout_limit=1024,
        )
    result = bounded_run(
        [sys.executable, "-c", "import sys;sys.stderr.write('x'*1000000);print('ok')"], timeout=3
    )
    assert result.stdout == b"ok\n" and len(result.stderr) == 64 * 1024


def test_fallback_respects_ignore_rules_and_searches_large_text(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("needle\n")
    (tmp_path / "large.txt").write_text("ordinary\n" * 300000 + "needle\n")
    options = dict(
        pattern="needle", workdir=str(tmp_path), search_path=None, include=None, timeout_ms=5000
    )
    matches = _python_grep_worker(**options)
    assert [m.path for m in matches] == ["large.txt"]
    assert matches[0].line_number == 300001
    assert {m.path for m in _python_grep_worker(**options, include_ignored=True)} == {
        "large.txt",
        "ignored.txt",
    }


def test_fallback_glob_separator_semantics():
    assert _matches_glob("src/a.py", "src/*.py")
    assert not _matches_glob("src/nested/a.py", "src/*.py")
    assert _matches_glob("src/nested/a.py", "src/**/*.py")
    assert _matches_glob("a.py", "**/*.py")


def test_pathological_regex_is_stopped_by_parent_deadline(tmp_path):
    (tmp_path / "bad.txt").write_text("a" * 200 + "!")
    with pytest.raises(SearchError) as error:
        _python_grep("(a+)+$", str(tmp_path), None, "*.txt", 500)
    assert error.value.code == "SEARCH_ABORTED"


def test_fallback_rejects_unsupported_lookaround(tmp_path):
    with pytest.raises(SearchError, match="requires ripgrep"):
        _python_grep_worker("(?=a)", str(tmp_path), None, None, 1000)


def test_all_builtins_have_declared_effects_and_generated_docs():
    registry = ToolRegistry()
    assert len(registry.list_tools()) == 49
    assert all(tool.effects is not None for tool in registry.list_tools())
    assert Path("docs/tools.md").read_text() == render_catalog(registry)
    registry.validate_arguments(
        "spawn_teammate",
        {
            "name": "n",
            "role": "architect",
            "prompt": "Review",
            "mode": "read_only",
            "allowed_tools": ["read"],
        },
    )
    registry.validate_arguments(
        "team_task_create", {"title": "T", "priority": "high", "dependencies": ["task"]}
    )
    registry.validate_arguments(
        "wait_agent", {"agent_ids": ["agent"], "wait_for": "any_settlement"}
    )


async def test_invalid_arguments_never_reach_prepared_hooks(tmp_path, monkeypatch):
    from coderai.tools.legacy.authorization import prepare_pre_tool_outcomes_async

    calls = []

    async def hook(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr("coderai.hooks.run_hook_point_async", hook)
    call = {
        "id": "bad",
        "function": {
            "name": "AskUserQuestion",
            "arguments": json.dumps({"questions": [{"question": "Q", "options": ["bad"]}]}),
        },
    }
    result = await prepare_pre_tool_outcomes_async("s", str(tmp_path), [call], {})
    assert not calls and result["bad"]["outcome"]["decision"] == "deny"


async def test_plugin_timeout_kills_child_processes(tmp_path):
    from coderai.plugin.tool import run_plugin_tool

    directory = tmp_path / "plugin"
    directory.mkdir()
    marker = directory / "survived"
    script = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1]]);time.sleep(20)"
    child = f"import time;from pathlib import Path;time.sleep(0.7);Path({str(marker)!r}).write_text('bad')"
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "name": "test",
                "version": "1",
                "tools": [
                    {
                        "name": "probe",
                        "description": "test",
                        "command": [sys.executable, "-c", script, child],
                        "parameters": {"type": "object"},
                    }
                ],
            }
        )
    )
    result = await run_plugin_tool("probe", {}, plugins_dir=tmp_path, timeout_s=0.2)
    assert not result.ok and result.metadata["code"] == "TOOL_TIMEOUT"
    await asyncio.sleep(0.8)
    assert not marker.exists()


async def test_executor_shutdown_drains_workers():
    from coderai.tools.legacy.executor import ToolExecutor

    executor = ToolExecutor(".")
    settled = []

    async def cleanup():
        await asyncio.sleep(0.01)
        settled.append(True)

    executor._handler_cleanup_tasks = {asyncio.create_task(cleanup())}
    await executor.aclose()
    assert settled


async def test_fallback_cancellation_stops_worker_before_deadline(tmp_path):
    import threading
    import time

    (tmp_path / "bad.txt").write_text("a" * 200 + "!")
    cancelled = threading.Event()
    started = time.monotonic()
    task = asyncio.create_task(
        asyncio.to_thread(
            _python_grep,
            "(a+)+$",
            str(tmp_path),
            None,
            "*.txt",
            10000,
            cancellation_event=cancelled,
        )
    )
    await asyncio.sleep(0.2)
    cancelled.set()
    with pytest.raises(SearchError) as error:
        await asyncio.wait_for(task, 2)
    assert error.value.code == "SEARCH_ABORTED"
    assert time.monotonic() - started < 2


def test_team_broadcast_and_direct_messages_stay_inside_scope():
    from coderai.teams.manager import TeamManager
    from coderai.teams.models import Teammate

    manager = TeamManager()
    one = Teammate("one", "same", "coder", owner_scope=("/project", "root1"))
    other = Teammate("other", "same", "coder", owner_scope=("/project", "root2"))
    manager._teammates = {"one": one, "other": other}
    manager.send_message("one", "all", "private")
    assert one.inbox and not other.inbox
    with pytest.raises(KeyError):
        manager.send_message("one", "other", "private")
    assert not other.inbox


@pytest.mark.parametrize("in_repository", [False, True])
@pytest.mark.parametrize("include_ignored", [False, True])
@pytest.mark.parametrize("include", [None, "*.txt", "!ignored.txt"])
def test_native_and_fallback_agree_on_supported_patterns(
    tmp_path, include_ignored, include, in_repository
):
    from coderai.tools.file._search_common import resolve_rg_path, run_ripgrep
    from coderai.tools.file.grep import build_grep_command, parse_grep_matches

    if not resolve_rg_path():
        pytest.skip("ripgrep not installed")
    if in_repository:
        (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("needle\n")
    (tmp_path / "visible.txt").write_text("before\nneedle\nafter\n")
    native = run_ripgrep(
        build_grep_command("needle", include=include, include_ignored=include_ignored),
        str(tmp_path),
    )
    expected = sorted(
        (m.path.lstrip("./"), m.line_number, m.line) for m in parse_grep_matches(native.stdout)
    )
    actual = sorted(
        (m.path, m.line_number, m.line)
        for m in _python_grep_worker(
            "needle", str(tmp_path), None, include, 1000, include_ignored=include_ignored
        )
    )
    assert actual == expected


def test_fallback_worker_failure_reports_stderr_instead_of_json_traceback(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        "coderai.utils.bounded_process.bounded_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, b"", b"Required worker dependency unavailable"
        ),
    )
    with pytest.raises(SearchError, match="Required worker dependency unavailable") as failure:
        _python_grep("needle", str(tmp_path), None, None, 1000)
    assert failure.value.code == "SEARCH_FAILED"


def test_plan_catalog_distinguishes_conditional_reads_and_masked_tools(tmp_path):
    from coderai.tools.legacy.catalog import inspect_tools
    from coderai.tools.legacy.types import ToolExecutionContext

    registry = ToolRegistry()
    context = ToolExecutionContext("s", str(tmp_path), plan_mode=True)
    rows = {row["name"]: row for row in inspect_tools(registry, context)}
    assert rows["read"]["available"] and not rows["read"]["conditional"]
    assert rows["goal"]["available"] and rows["goal"]["conditional"]
    assert "action=status only" in rows["goal"]["restrictions"]
    assert rows["write"]["conditional"]
    registry.restrict({"allow": ["read"]})
    restricted = {row["name"]: row for row in inspect_tools(registry, context)}
    assert not restricted["goal"]["available"] and not restricted["goal"]["conditional"]
