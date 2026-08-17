"""Comprehensive unit tests for CoderAI modernized tool architecture (port of deepcode tool tests)."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

import pytest

from coderai.core.common.validate import (
    execute_validated_tool,
    semantic_boolean,
    semantic_integer,
)
from coderai.core.mcp.client import create_mcp_spawn_spec
from coderai.core.mcp.manager import (
    build_mcp_namespaced_name,
)
from coderai.core.state import (
    clear_session_state,
)
from coderai.core.tools.ask_user_question import handle as ask_handle
from coderai.core.tools.bash import clear_session_working_dir, handle as bash_handle
from coderai.core.tools.edit import handle as edit_handle
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.read import handle as read_handle
from coderai.core.tools.types import (
    BackgroundProcessCompletion,
    ToolExecutionContext,
    ToolExecutionHooks,
    ToolResult,
)
from coderai.core.tools.update_plan import handle as plan_handle
from coderai.core.tools.write import handle as write_handle


# ==========================================
# 1. Validation Utilities (validate.py)
# ==========================================


def test_semantic_boolean():
    assert semantic_boolean(True) is True
    assert semantic_boolean(False) is False
    assert semantic_boolean("true") is True
    assert semantic_boolean("True") is True
    assert semantic_boolean("1") is True
    assert semantic_boolean("yes") is True
    assert semantic_boolean("false") is False
    assert semantic_boolean("0") is False
    assert semantic_boolean("no") is False
    assert semantic_boolean(None, default=True) is True
    assert semantic_boolean(None, default=False) is False


def test_semantic_integer():
    ok, val, err = semantic_integer(5, "count")
    assert ok and val == 5 and err is None

    ok, val, err = semantic_integer("10", "count")
    assert ok and val == 10 and err is None

    ok, val, err = semantic_integer(None, "count")
    assert ok and val is None and err is None

    ok, val, err = semantic_integer("abc", "count")
    assert not ok and val is None and "must be a number" in err

    ok, val, err = semantic_integer(3.5, "count")
    assert not ok and val is None and "must be an integer" in err

    ok, val, err = semantic_integer(0, "count", min_val=1)
    assert not ok and val is None and "must be >= 1" in err


def test_execute_validated_tool():
    def validator(args: dict[str, Any]):
        if "req" not in args:
            return False, {}, "req is required"
        return True, args, None

    def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult(ok=True, name="test", output=args["req"])

    # Validation failure
    res = execute_validated_tool("test", {}, {}, handler, validator=validator)
    assert not res.ok
    assert "InputValidationError: req is required" in res.error

    # Validation success
    res_ok = execute_validated_tool("test", {"req": "hello"}, {}, handler, validator=validator)
    assert res_ok.ok
    assert res_ok.output == "hello"


# ==========================================
# 2. Bash Tool (bash.py)
# ==========================================


def test_bash_basic_execution(tmp_path: pathlib.Path):
    session_id = "test_bash_sess"
    clear_session_working_dir(session_id)
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    res = bash_handle({"command": "echo 'Hello CoderAI'"}, ctx)
    assert res.ok
    assert "Hello CoderAI" in (res.output or "")
    assert res.metadata["exitCode"] == 0
    assert res.metadata["cwd"] is not None


def test_bash_cwd_tracking(tmp_path: pathlib.Path):
    session_id = "test_bash_cwd"
    clear_session_working_dir(session_id)
    subdir = tmp_path / "subfolder"
    subdir.mkdir()
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    # cd into subdirectory
    res = bash_handle({"command": f"cd {subdir.name}"}, ctx)
    assert res.ok
    assert res.metadata["cwd"] == str(subdir)

    # next command runs from updated cwd
    res2 = bash_handle({"command": "pwd"}, ctx)
    assert res2.ok
    assert str(subdir) in (res2.output or "")


def test_bash_streaming_and_hooks(tmp_path: pathlib.Path):
    session_id = "test_bash_hooks"
    started = []
    exited = []
    stdout_lines = []
    timeout_controls = []

    hooks = ToolExecutionHooks(
        on_process_start=lambda pid, cmd: started.append((pid, cmd)),
        on_process_exit=lambda pid: exited.append(pid),
        on_process_stdout=lambda pid, line: stdout_lines.append(line),
        on_process_timeout_control=lambda pid, ctrl: timeout_controls.append((pid, ctrl)),
    )

    ctx = ToolExecutionContext(
        session_id=session_id,
        project_root=str(tmp_path),
        on_process_start=hooks.on_process_start,
        on_process_exit=hooks.on_process_exit,
        on_process_stdout=hooks.on_process_stdout,
        on_process_timeout_control=hooks.on_process_timeout_control,
    )

    res = bash_handle({"command": "echo 'line 1'; echo 'line 2'"}, ctx)
    assert res.ok
    assert len(started) == 1
    assert len(exited) == 1
    assert any("line 1" in line for line in stdout_lines)
    assert any("line 2" in line for line in stdout_lines)
    assert len(timeout_controls) >= 1


def test_bash_background_execution_lifecycle(tmp_path: pathlib.Path):
    session_id = "test_bash_bg"
    completed_events: list[BackgroundProcessCompletion] = []

    ctx = ToolExecutionContext(
        session_id=session_id,
        project_root=str(tmp_path),
        on_background_process_complete=lambda comp: completed_events.append(comp),
    )

    res = bash_handle({"command": "echo 'bg finished'", "run_in_background": True}, ctx)
    assert res.ok
    assert "Command running in background with ID:" in (res.output or "")
    assert res.metadata["runInBackground"] is True
    assert res.metadata["outputPath"] is not None

    # Wait for background completion event
    for _ in range(50):
        if completed_events:
            break
        time.sleep(0.1)

    assert len(completed_events) == 1
    comp = completed_events[0]
    assert comp.ok
    assert comp.exit_code == 0
    assert pathlib.Path(comp.output_path).exists()
    assert "bg finished" in pathlib.Path(comp.output_path).read_text()


# ==========================================
# 3. Read Tool (read.py)
# ==========================================


def test_read_formatting_and_snippets(tmp_path: pathlib.Path):
    session_id = "test_read_sess"
    clear_session_state(session_id)
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    p = tmp_path / "sample.txt"
    p.write_text("first line\nsecond line\nthird line\nfourth line\n")

    # Read full file
    res = read_handle({"file_path": str(p)}, ctx)
    assert res.ok
    assert "     1\tfirst line" in (res.output or "")
    assert "     2\tsecond line" in (res.output or "")
    assert res.metadata["snippet"]["id"].startswith("full_file_")

    # Read partial file with offset and limit
    res_partial = read_handle({"file_path": str(p), "offset": 2, "limit": 2}, ctx)
    assert res_partial.ok
    assert "     2\tsecond line" in (res_partial.output or "")
    assert "     3\tthird line" in (res_partial.output or "")
    assert "first line" not in (res_partial.output or "")
    assert res_partial.metadata["snippet"]["id"].startswith("snippet_")


def test_read_jupyter_notebook(tmp_path: pathlib.Path):
    session_id = "test_read_nb"
    clear_session_state(session_id)
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    nb_path = tmp_path / "test.ipynb"
    nb_content = {
        "cells": [
            {
                "cell_type": "code",
                "source": ["x = 42\n", "print(x)"],
                "outputs": [{"output_type": "stream", "text": ["42\n"]}],
            },
            {"cell_type": "markdown", "source": ["# Analysis Heading"]},
        ]
    }
    nb_path.write_text(json.dumps(nb_content))

    res = read_handle({"file_path": str(nb_path)}, ctx)
    assert res.ok
    assert "# Cell 1 (code)" in (res.output or "")
    assert "x = 42" in (res.output or "")
    assert "# Output 1 (stream)" in (res.output or "")
    assert "# Cell 2 (markdown)" in (res.output or "")


def test_read_image_follow_up_messages(tmp_path: pathlib.Path):
    session_id = "test_read_img"
    clear_session_state(session_id)
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    img_path = tmp_path / "icon.png"
    img_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    img_path.write_bytes(img_bytes)

    res = read_handle({"file_path": str(img_path)}, ctx)
    assert res.ok
    assert res.output == "File loaded."
    assert res.metadata["mime"] == "image/png"
    assert len(res.follow_up_messages) == 1
    fum = res.follow_up_messages[0]
    assert fum.role == "system"
    assert "icon.png" in fum.content
    assert fum.content_params[0]["type"] == "image_url"
    assert "data:image/png;base64," in fum.content_params[0]["image_url"]["url"]


def test_read_ambiguous_path_error(tmp_path: pathlib.Path):
    session_id = "test_read_ambig"
    clear_session_state(session_id)
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()
    (dir1 / "common.py").write_text("x = 1")
    (dir2 / "common.py").write_text("x = 2")

    res = read_handle({"file_path": "common.py"}, ctx)
    assert not res.ok
    assert "file_path is ambiguous" in res.error


# ==========================================
# 4. Write Tool (write.py)
# ==========================================


def test_write_new_file(tmp_path: pathlib.Path):
    session_id = "test_write_new"
    clear_session_state(session_id)
    p = tmp_path / "created.py"
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    res = write_handle({"file_path": str(p), "content": "print('hello world')\n"}, ctx)
    assert res.ok
    assert res.output == "Created file."
    assert res.metadata["type"] == "create"
    assert res.metadata["diff_preview"] is not None
    assert p.read_text() == "print('hello world')\n"


def test_write_existing_file_safety_checks(tmp_path: pathlib.Path):
    session_id = "test_write_safety"
    clear_session_state(session_id)
    p = tmp_path / "existing.py"
    p.write_text("line 1\nline 2\n")
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    # Attempt overwrite without reading first -> must fail
    res_unseen = write_handle({"file_path": str(p), "content": "line 1 modified\n"}, ctx)
    assert not res_unseen.ok
    assert "Must read the full existing file before writing" in res_unseen.error

    # Read only partial file -> must fail
    read_handle({"file_path": str(p), "offset": 1, "limit": 1}, ctx)
    res_partial = write_handle({"file_path": str(p), "content": "line 1 modified\n"}, ctx)
    assert not res_partial.ok
    assert "Must read the full existing file before writing" in res_partial.error

    # Read full file -> success
    read_handle({"file_path": str(p)}, ctx)
    res_full = write_handle({"file_path": str(p), "content": "line 1 modified\nline 2\n"}, ctx)
    assert res_full.ok
    assert res_full.output == "Updated file."


def test_write_json_auto_repair(tmp_path: pathlib.Path):
    session_id = "test_write_json"
    clear_session_state(session_id)
    p = tmp_path / "config.json"
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    # Pass json dictionary instead of string
    res = write_handle({"file_path": str(p), "content": {"key": "val", "num": 100}}, ctx)
    assert res.ok
    assert res.metadata.get("input_repaired") is True
    assert res.metadata.get("repair_kind") == "json-stringify-content"
    parsed = json.loads(p.read_text())
    assert parsed["key"] == "val"
    assert parsed["num"] == 100


# ==========================================
# 5. Edit Tool (edit.py)
# ==========================================


def test_edit_exact_match(tmp_path: pathlib.Path):
    session_id = "test_edit_exact"
    clear_session_state(session_id)
    p = tmp_path / "target.py"
    p.write_text("def add(a, b):\n    return a - b\n")
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    r_res = read_handle({"file_path": str(p)}, ctx)
    snip_id = r_res.metadata["snippet"]["id"]

    e_res = edit_handle(
        {
            "snippet_id": snip_id,
            "file_path": str(p),
            "old_string": "    return a - b",
            "new_string": "    return a + b",
        },
        ctx,
    )
    assert e_res.ok
    assert "Replaced 1 occurrence" in (e_res.output or "")
    assert e_res.metadata["matched_via"] == "exact"
    assert p.read_text() == "def add(a, b):\n    return a + b\n"


def test_edit_leading_tab_stripping(tmp_path: pathlib.Path):
    session_id = "test_edit_tab"
    clear_session_state(session_id)
    p = tmp_path / "target.py"
    p.write_text("const x = 1;\nconst y = 2;\nconst z = 3;\n")
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    r_res = read_handle({"file_path": str(p)}, ctx)
    snip_id = r_res.metadata["snippet"]["id"]

    # When old_string has leading tab from copied read lines
    e_res = edit_handle(
        {
            "snippet_id": snip_id,
            "old_string": "const y = 2;\n\tconst z = 3;",
            "new_string": "const y = 20;\n\tconst z = 30;",
        },
        ctx,
    )
    assert e_res.ok
    assert e_res.metadata["matched_via"] == "line_leading_tab_correction"
    assert "const y = 20;\nconst z = 30;\n" in p.read_text()


def test_edit_multi_match_candidates(tmp_path: pathlib.Path):
    session_id = "test_edit_multi"
    clear_session_state(session_id)
    p = tmp_path / "multi.py"
    p.write_text("item = None\nitem = None\nitem = None\n")
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    r_res = read_handle({"file_path": str(p)}, ctx)
    snip_id = r_res.metadata["snippet"]["id"]

    # Without replace_all -> returns ambiguity error with candidate snippets
    e_res = edit_handle(
        {
            "snippet_id": snip_id,
            "old_string": "item = None",
            "new_string": "item = 42",
        },
        ctx,
    )
    assert not e_res.ok
    assert "old_string is not unique" in e_res.error
    assert e_res.metadata["match_count"] == 3
    assert len(e_res.metadata["candidates"]) == 3
    assert e_res.metadata["candidates"][0]["snippet_id"] is not None


def test_edit_replace_all_guards(tmp_path: pathlib.Path):
    session_id = "test_edit_rep_all"
    clear_session_state(session_id)
    p = tmp_path / "guards.py"
    p.write_text("x = 1\nx = 1\nx = 1\n")
    ctx = {"session_id": session_id, "project_root": str(tmp_path)}

    r_res = read_handle({"file_path": str(p)}, ctx)
    snip_id = r_res.metadata["snippet"]["id"]

    # Short string (<40 chars) without expected_occurrences -> guarded
    e_guarded = edit_handle(
        {
            "snippet_id": snip_id,
            "old_string": "x = 1",
            "new_string": "x = 2",
            "replace_all": True,
        },
        ctx,
    )
    assert not e_guarded.ok
    assert "provide expected_occurrences" in e_guarded.error

    # Wrong expected_occurrences
    e_wrong = edit_handle(
        {
            "snippet_id": snip_id,
            "old_string": "x = 1",
            "new_string": "x = 2",
            "replace_all": True,
            "expected_occurrences": 2,
        },
        ctx,
    )
    assert not e_wrong.ok
    assert "replace_all expected 2 occurrence(s), but found 3" in e_wrong.error

    # Correct expected_occurrences
    e_ok = edit_handle(
        {
            "snippet_id": snip_id,
            "old_string": "x = 1",
            "new_string": "x = 2",
            "replace_all": True,
            "expected_occurrences": 3,
        },
        ctx,
    )
    assert e_ok.ok
    assert "Replaced 3 occurrence(s)" in e_ok.output
    assert p.read_text() == "x = 2\nx = 2\nx = 2\n"


# ==========================================
# 6. AskUserQuestion & UpdatePlan
# ==========================================


def test_ask_user_question_multi_select():
    ctx = {"session_id": "test_ask_multi", "project_root": "/tmp"}
    res = ask_handle(
        {
            "questions": [
                {
                    "question": "Which frameworks should we test?",
                    "multiSelect": True,
                    "options": [
                        {"label": "React", "description": "Web library"},
                        {"label": "Vue", "description": "Progressive framework"},
                    ],
                }
            ]
        },
        ctx,
    )
    assert res.ok
    assert res.await_user_response is True
    assert "Mode: multi-select" in res.output
    assert "- React" in res.output
    assert "Web library" in res.output


def test_update_plan_validation():
    ctx = {"session_id": "test_plan_val", "project_root": "/tmp"}
    res_bad = plan_handle({"plan": ""}, ctx)
    assert not res_bad.ok
    assert "plan must be a non-empty string" in res_bad.error

    plan_content = "1. Read repo\n2. Modernize tools\n3. Run tests\n"
    res_good = plan_handle({"plan": plan_content, "explanation": "Starting modernization"}, ctx)
    assert res_good.ok
    assert res_good.output == "Plan updated."
    assert res_good.metadata["plan"] == plan_content
    assert res_good.metadata["explanation"] == "Starting modernization"


# ==========================================
# 7. ToolExecutor & Alias Mapping
# ==========================================


@pytest.mark.asyncio
async def test_tool_executor_aliases_and_lifecycle(tmp_path: pathlib.Path):
    p = tmp_path / "alias_test.py"
    p.write_text("print('hello')\n")
    session_id = "test_exec_aliases"
    clear_session_state(session_id)

    executor = ToolExecutor(project_root=str(tmp_path))

    # Read via "Read" alias
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "Read",
                "arguments": json.dumps({"file_path": str(p)}),
            },
        }
    ]
    results = await executor.execute_tool_calls(session_id, calls)
    assert len(results) == 1
    assert results[0]["result"]["ok"] is True
    assert "hello" in results[0]["result"]["output"]


@pytest.mark.asyncio
async def test_tool_executor_cancellation(tmp_path: pathlib.Path):
    session_id = "test_exec_cancel"
    stop_flag = False

    hooks = ToolExecutionHooks(should_stop=lambda: stop_flag)
    executor = ToolExecutor(project_root=str(tmp_path))

    stop_flag = True
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": "echo 1"}),
            },
        }
    ]
    results = await executor.execute_tool_calls(session_id, calls, hooks=hooks)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_tool_executor_dispatches_mcp_tools(tmp_path: pathlib.Path):
    class FakeMcp:
        def is_mcp_tool(self, name: str) -> bool:
            return name.startswith("mcp__")

        async def execute_mcp_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
            return ToolResult(ok=True, name=name, output=f"mcp:{args.get('q')}")

    executor = ToolExecutor(project_root=str(tmp_path), mcp_manager=FakeMcp())
    results = await executor.execute_tool_calls(
        "mcp-sess",
        [
            {
                "id": "call_mcp",
                "type": "function",
                "function": {
                    "name": "mcp__memory__search",
                    "arguments": json.dumps({"q": "hello"}),
                },
            }
        ],
    )
    assert len(results) == 1
    assert results[0]["result"]["ok"] is True
    assert results[0]["result"]["output"] == "mcp:hello"

    missing = ToolExecutor(project_root=str(tmp_path))
    unknown = await missing.execute_tool_calls(
        "mcp-sess",
        [
            {
                "id": "call_mcp",
                "type": "function",
                "function": {
                    "name": "mcp__memory__search",
                    "arguments": "{}",
                },
            }
        ],
    )
    assert unknown[0]["result"]["ok"] is False
    assert "Unknown tool" in (unknown[0]["result"]["error"] or "")


# ==========================================
# 8. MCP Client & Manager
# ==========================================


def test_mcp_namespacing_and_sanitization():
    # Regular tool name
    ns1 = build_mcp_namespaced_name("postgres", "query_db")
    assert ns1 == "mcp__postgres__query_db"

    # Special characters
    ns2 = build_mcp_namespaced_name("my-server@v1", "run/query.test")
    assert "mcp__my-server_v1__run_query_test" in ns2

    # Long tool name truncated to <= 64 chars
    long_server = "extremely_long_server_name_that_exceeds_normal_lengths"
    long_tool = "a_very_long_tool_name_designed_to_test_64_character_bounds"
    ns_long = build_mcp_namespaced_name(long_server, long_tool)
    assert len(ns_long) <= 64
    assert ns_long.startswith("mcp__")


def test_mcp_spawn_spec():
    spec = create_mcp_spawn_spec(
        "npx", ["-y", "@modelcontextprotocol/server-memory"], platform="darwin"
    )
    assert spec["command"] == "npx"
    assert spec["args"] == ["-y", "@modelcontextprotocol/server-memory"]
    assert spec["shell"] is False
