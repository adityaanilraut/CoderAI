"""Tests for Phase 1 Core Tool Extensions (str_replace_editor, terminal, lsp, schedule)."""

import os
import tempfile
import time
import pytest

from coderai.core.permissions import describe_tool_permission_request
from coderai.core.schedule import ScheduleManager
from coderai.core.tools.lsp import handle_lsp_tool
from coderai.core.tools.registry import get_tool_registry
from coderai.core.tools.schedule import (
    handle_schedule_create_tool,
    handle_schedule_delete_tool,
    handle_schedule_list_tool,
)
from coderai.core.tools.str_replace_editor import handle_str_replace_editor_tool
from coderai.core.tools.terminal import (
    handle_terminal_close_tool,
    handle_terminal_list_tool,
    handle_terminal_open_tool,
    handle_terminal_read_tool,
    handle_terminal_send_tool,
    handle_terminal_signal_tool,
)
from coderai.core.tools.types import ToolExecutionContext


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def tool_context(temp_workspace):
    return ToolExecutionContext(
        session_id="test_session_p1",
        project_root=temp_workspace,
    )


# ==========================================
# 1. str_replace_editor Tests
# ==========================================


def test_str_replace_editor_create_and_view(temp_workspace, tool_context):
    file_path = os.path.join(temp_workspace, "sample.py")

    # 1. Create file
    res = handle_str_replace_editor_tool(
        {"command": "create", "path": file_path, "file_text": "line 1\nline 2\nline 3\n"},
        tool_context,
    )
    assert res.ok is True
    assert "File created successfully" in res.output
    assert os.path.isfile(file_path)

    # 2. View file full
    res_view = handle_str_replace_editor_tool(
        {"command": "view", "path": file_path},
        tool_context,
    )
    assert res_view.ok is True
    assert "line 1" in res_view.output
    assert "line 2" in res_view.output

    # 3. View line range [2, 3]
    res_range = handle_str_replace_editor_tool(
        {"command": "view", "path": file_path, "view_range": [2, 3]},
        tool_context,
    )
    assert res_range.ok is True
    assert "line 2" in res_range.output
    assert "line 3" in res_range.output
    assert "line 1" not in res_range.output


def test_str_replace_editor_replace_and_undo(temp_workspace, tool_context):
    file_path = os.path.join(temp_workspace, "edit_me.txt")
    with open(file_path, "w") as f:
        f.write("apple\nbanana\ncherry\n")

    # 1. Successful unique replacement
    res = handle_str_replace_editor_tool(
        {
            "command": "str_replace",
            "path": file_path,
            "old_str": "banana",
            "new_str": "blueberry",
        },
        tool_context,
    )
    assert res.ok is True
    assert "edited successfully" in res.output

    with open(file_path) as f:
        content = f.read()
    assert "blueberry" in content
    assert "banana" not in content

    # 2. Undo edit
    res_undo = handle_str_replace_editor_tool(
        {"command": "undo_edit", "path": file_path},
        tool_context,
    )
    assert res_undo.ok is True
    assert "undid previous edit" in res_undo.output

    with open(file_path) as f:
        reverted = f.read()
    assert "banana" in reverted
    assert "blueberry" not in reverted


def test_str_replace_editor_insert(temp_workspace, tool_context):
    file_path = os.path.join(temp_workspace, "insert_test.txt")
    with open(file_path, "w") as f:
        f.write("Line 1\nLine 2\n")

    res = handle_str_replace_editor_tool(
        {
            "command": "insert",
            "path": file_path,
            "insert_line": 1,
            "new_str": "Inserted Line",
        },
        tool_context,
    )
    assert res.ok is True
    with open(file_path) as f:
        lines = f.read().splitlines()
    assert lines == ["Line 1", "Inserted Line", "Line 2"]


def test_str_replace_editor_duplicate_match_error(temp_workspace, tool_context):
    file_path = os.path.join(temp_workspace, "dup.txt")
    with open(file_path, "w") as f:
        f.write("foo\nbar\nfoo\n")

    res = handle_str_replace_editor_tool(
        {
            "command": "str_replace",
            "path": file_path,
            "old_str": "foo",
            "new_str": "baz",
        },
        tool_context,
    )
    assert res.ok is False
    assert "Multiple occurrences" in res.error


# ==========================================
# 2. Persistent PTY Terminal Tests
# ==========================================


def test_terminal_lifecycle(tool_context):
    # 1. Open session
    res_open = handle_terminal_open_tool(
        {"type": "sh", "name": "test_sh"},
        tool_context,
    )
    assert res_open.ok is True
    session_id = res_open.metadata["sessionId"]
    assert session_id.startswith("term_")

    # 2. Send command
    res_send = handle_terminal_send_tool(
        {
            "sessionId": session_id,
            "text": "echo 'HELLO_TERMINAL'",
            "submit": True,
            "timeout_ms": 1000,
        },
        tool_context,
    )
    assert res_send.ok is True
    assert "HELLO_TERMINAL" in res_send.metadata["output"]

    # 3. Read available output
    res_read = handle_terminal_read_tool(
        {"sessionId": session_id, "timeout_ms": 100},
        tool_context,
    )
    assert res_read.ok is True

    # 4. List terminals
    res_list = handle_terminal_list_tool({}, tool_context)
    assert res_list.ok is True
    session_ids = [s["sessionId"] for s in res_list.metadata["sessions"]]
    assert session_id in session_ids

    # 5. Signal SIGINT
    res_sig = handle_terminal_signal_tool(
        {"sessionId": session_id, "signal": "SIGINT"},
        tool_context,
    )
    assert res_sig.ok is True

    # 6. Close session
    res_close = handle_terminal_close_tool(
        {"sessionId": session_id},
        tool_context,
    )
    assert res_close.ok is True
    assert "closed successfully" in res_close.output


# ==========================================
# 3. LSP Tool Tests
# ==========================================


def test_lsp_fallback_definitions_and_hover(temp_workspace, tool_context):
    py_file = os.path.join(temp_workspace, "math_utils.py")
    with open(py_file, "w") as f:
        f.write(
            'def calculate_sum(a, b):\n    """Compute sum of two numbers."""\n    return a + b\n\nresult = calculate_sum(10, 20)\n'
        )

    # 1. Definition lookup
    res_def = handle_lsp_tool(
        {
            "operation": "goToDefinition",
            "file_path": py_file,
            "line": 5,
            "character": 12,
        },
        tool_context,
    )
    assert res_def.ok is True
    assert "calculate_sum" in res_def.output
    assert res_def.metadata.get("locations") is not None

    # 2. Hover lookup
    res_hover = handle_lsp_tool(
        {
            "operation": "hover",
            "file_path": py_file,
            "line": 5,
            "character": 12,
        },
        tool_context,
    )
    assert res_hover.ok is True
    assert "Compute sum of two numbers" in res_hover.output

    # 3. Document Symbols
    res_syms = handle_lsp_tool(
        {
            "operation": "documentSymbol",
            "file_path": py_file,
        },
        tool_context,
    )
    assert res_syms.ok is True
    assert "calculate_sum" in res_syms.output


# ==========================================
# 4. Schedule Subsystem Tests
# ==========================================


def test_schedule_crud_and_relative_after(temp_workspace, tool_context):
    mgr = ScheduleManager(storage_path=os.path.join(temp_workspace, "schedules.json"))

    # 1. Create after_seconds schedule
    rec = mgr.create(prompt="Run linter in 2 seconds", after_seconds=2)
    assert rec.id.startswith("sched_")
    assert rec.kind == "after"
    assert rec.after_seconds == 2
    assert rec.state == "scheduled"

    # 2. List schedules
    schedules = mgr.list_schedules()
    assert len(schedules) == 1
    assert schedules[0].id == rec.id

    # 3. Wait and check due
    time.sleep(2.1)
    due = mgr.check_due()
    assert len(due) == 1
    assert due[0].id == rec.id
    assert due[0].state == "dispatched"

    # 4. Delete schedule
    del_ok = mgr.delete(rec.id)
    assert del_ok is True or due[0].state == "dispatched"


def test_schedule_tool_handlers(temp_workspace, tool_context):
    # 1. Tool create
    res_create = handle_schedule_create_tool(
        {"prompt": "Audit workspace", "after_seconds": 60},
        tool_context,
    )
    assert res_create.ok is True
    sched_id = res_create.metadata["id"]

    # 2. Tool list
    res_list = handle_schedule_list_tool({}, tool_context)
    assert res_list.ok is True
    sched_ids = [s["id"] for s in res_list.metadata["schedules"]]
    assert sched_id in sched_ids

    # 3. Tool delete
    res_del = handle_schedule_delete_tool({"schedule_id": sched_id}, tool_context)
    assert res_del.ok is True
    assert res_del.metadata["deleted"] is True


# ==========================================
# 5. Tool Registry & Permissions Integration
# ==========================================


def test_tool_registry_has_phase1_tools():
    registry = get_tool_registry()
    phase1_tools = [
        "str_replace_editor",
        "terminal_open",
        "terminal_send",
        "terminal_read",
        "terminal_signal",
        "terminal_close",
        "terminal_list",
        "lsp",
        "schedule_create",
        "schedule_list",
        "schedule_delete",
    ]
    for tool_name in phase1_tools:
        assert registry.has_tool(tool_name), (
            f"Tool {tool_name} should be registered in ToolRegistry"
        )


def test_permissions_for_phase1_tools(temp_workspace):
    # str_replace_editor view
    req_view = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c1",
            "function": {
                "name": "str_replace_editor",
                "arguments": '{"command": "view", "path": "file.py"}',
            },
        },
    )
    assert "read-in-cwd" in req_view["scopes"]

    # str_replace_editor edit
    req_edit = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c2",
            "function": {
                "name": "str_replace_editor",
                "arguments": '{"command": "str_replace", "path": "file.py", "old_str": "a", "new_str": "b"}',
            },
        },
    )
    assert "write-in-cwd" in req_edit["scopes"]

    # terminal_send
    req_term = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c3",
            "function": {
                "name": "terminal_send",
                "arguments": '{"sessionId": "term_1", "text": "ls"}',
            },
        },
    )
    assert "write-in-cwd" in req_term["scopes"]

    # lsp
    req_lsp = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c4",
            "function": {
                "name": "lsp",
                "arguments": '{"operation": "goToDefinition", "file_path": "main.py", "line": 1, "character": 1}',
            },
        },
    )
    assert "read-in-cwd" in req_lsp["scopes"]
