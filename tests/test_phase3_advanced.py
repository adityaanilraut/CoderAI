"""Comprehensive tests for Phase 3: Code Mode, Session Query (FTS), and Cross-Platform Shell (pwsh).

Covers:
1. Code Mode Engine & Stateful Sandbox (state retention, tools, evaluation, error recovery).
2. Session Full-Text Search & History Query Indexer (indexing, BM25 scoring, filters, tool handler).
3. Cross-Platform PowerShell Subsystem (pwsh tool, background jobs, timeouts).
4. Tool Registry & Permissions integration.
"""

import json
import pathlib
import tempfile
import pytest

from coderai.core.code_mode import (
    CodeModeSandbox,
    handle_code_mode_tool,
)
from coderai.core.jobs import get_job_store
from coderai.core.permissions import describe_tool_permission_request
from coderai.core.session_query import (
    SessionIndex,
    handle_session_query_tool,
)
from coderai.core.tools.pwsh import handle_pwsh_tool
from coderai.core.tools.registry import get_tool_registry
from coderai.core.tools.types import ToolExecutionContext


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def tool_context(temp_workspace):
    return ToolExecutionContext(
        session_id="test_phase3_session",
        project_root=temp_workspace,
    )


# =====================================================================
# 1. Code Mode Engine Tests
# =====================================================================


@pytest.mark.asyncio
async def test_code_mode_sandbox_basic_and_state_retention(temp_workspace):
    sandbox = CodeModeSandbox(temp_workspace)

    # 1. Turn 1: Assign variables and functions
    res1 = await sandbox.execute("""
x = 10
y = 20
def add(a, b):
    return a + b
print(f"Computed sum: {add(x, y)}")
add(x, y)
""")
    assert res1.error is None
    assert "Computed sum: 30" in res1.stdout
    assert res1.result == 30
    assert "x" in res1.variables
    assert "y" in res1.variables
    assert "add" in res1.variables

    # 2. Turn 2: Retain state from Turn 1
    res2 = await sandbox.execute("""
z = add(x, y) * 2
z
""")
    assert res2.error is None
    assert res2.result == 60
    assert "z" in res2.variables

    # 3. Turn 3: Reset state
    sandbox.reset()
    res3 = await sandbox.execute("x")
    assert res3.error is not None
    assert "NameError" in res3.error


@pytest.mark.asyncio
async def test_code_mode_workspace_tools(temp_workspace):
    sandbox = CodeModeSandbox(temp_workspace)

    # Use workspace tools inside python code
    code = """
write_file("hello.txt", "line 1\\nline 2\\nline 3\\n")
content = read_file("hello.txt")
edit_file("hello.txt", "line 2", "line TWO")
updated = read_file("hello.txt")
files = glob_search("*.txt")
{"content": content, "updated": updated, "files": files}
"""
    res = await sandbox.execute(code)
    assert res.error is None
    assert isinstance(res.result, dict)
    assert "line 1\nline 2\nline 3\n" == res.result["content"]
    assert "line TWO" in res.result["updated"]
    assert "hello.txt" in res.result["files"]


@pytest.mark.asyncio
async def test_code_mode_error_and_timeout(temp_workspace, tool_context):
    sandbox = CodeModeSandbox(temp_workspace)

    # 1. Syntax Error
    res_syn = await sandbox.execute("def invalid syntax:")
    assert res_syn.error is not None
    assert "SyntaxError" in res_syn.error

    # 2. Runtime Error
    res_run = await sandbox.execute("1 / 0")
    assert res_run.error is not None
    assert "ZeroDivisionError" in res_run.error

    # 3. Timeout
    res_time = await sandbox.execute("import time; time.sleep(0.5)", timeout_seconds=0.1)
    assert res_time.error is not None
    assert "TimeoutError" in res_time.error

    # 4. Tool handler
    tool_res = await handle_code_mode_tool({"code": "val = 42; val"}, tool_context)
    assert tool_res.ok is True
    assert "42" in tool_res.output
    assert tool_res.metadata["result"] == "42"

    # 5. Missing code argument
    tool_res_err = await handle_code_mode_tool({}, tool_context)
    assert tool_res_err.ok is False
    assert "Missing required argument" in tool_res_err.error


# =====================================================================
# 2. Session Query & Full-Text Search (FTS) Tests
# =====================================================================


def test_session_index_in_memory():
    index = SessionIndex("/tmp/test_workspace")

    # Index some sample messages
    messages = [
        {
            "id": "m1",
            "role": "user",
            "content": "How do I configure Postgres database connections in settings.py?",
        },
        {
            "id": "m2",
            "role": "assistant",
            "content": "You can configure Postgres by setting DATABASE_URL in settings.py with connection pooling.",
        },
        {
            "id": "m3",
            "role": "tool",
            "name": "bash",
            "content": "pytest tests/test_auth.py: 15 passed in 1.2s.",
        },
        {"id": "m4", "role": "user", "content": "Please write an authentication unit test."},
    ]

    index.index_messages(messages, session_id="session_abc")

    # 1. Search for postgres
    res = index.search("postgres database")
    assert len(res) >= 2
    assert res[0].session_id == "session_abc"
    assert "Postgres" in res[0].content_snippet or "postgres" in res[0].content_snippet

    # 2. Search for pytest with role filter
    res_tool = index.search("pytest auth", role="tool")
    assert len(res_tool) == 1
    assert res_tool[0].tool_name == "bash"
    assert "15 passed" in res_tool[0].content_snippet

    # 3. Search with non-matching query
    res_empty = index.search("kubernetes ingress controller")
    assert len(res_empty) == 0


@pytest.mark.asyncio
async def test_session_query_workspace_and_tool(temp_workspace, tool_context):
    # Create fake session JSONL on disk
    sessions_dir = pathlib.Path(temp_workspace) / ".coderAI" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = sessions_dir / "sess_123.jsonl"

    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "id": "msg_1",
                    "role": "user",
                    "content": "Build a React dashboard with TailwindCSS.",
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "id": "msg_2",
                    "role": "assistant",
                    "content": "Created App.tsx and Dashboard.tsx with responsive grid.",
                }
            )
            + "\n"
        )

    # 1. Search using tool handler
    res = await handle_session_query_tool({"query": "React dashboard"}, tool_context)
    assert res.ok is True
    assert "React dashboard" in res.output
    assert len(res.metadata["results"]) >= 1

    # 2. Query not found
    res_empty = await handle_session_query_tool({"query": "unrelated_query_no_match"}, tool_context)
    assert res_empty.ok is True
    assert "No matching session messages" in res_empty.output

    # 3. Missing query parameter
    res_err = await handle_session_query_tool({}, tool_context)
    assert res_err.ok is False
    assert "Missing required argument" in res_err.error


# =====================================================================
# 3. Cross-Platform PowerShell Subsystem Tests
# =====================================================================


@pytest.mark.asyncio
async def test_pwsh_tool_execution(tool_context):
    # 1. Test missing command
    res_err = await handle_pwsh_tool({}, tool_context)
    assert res_err.ok is False
    assert "Missing required argument 'command'" in res_err.error

    # 2. Check if pwsh is available on host
    import shutil

    has_pwsh = bool(
        shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    )

    if has_pwsh:
        # Synchronous execution
        res = await handle_pwsh_tool(
            {
                "command": "Write-Output 'Hello from PowerShell'",
                "description": "Echo test",
                "sideEffects": ["read-in-cwd"],
            },
            tool_context,
        )
        assert res.ok is True
        assert "Hello from PowerShell" in res.output

        # Background job execution
        bg_res = await handle_pwsh_tool(
            {
                "command": "Start-Sleep -Milliseconds 100; Write-Output 'Done'",
                "run_in_background": True,
                "sideEffects": ["read-in-cwd"],
            },
            tool_context,
        )
        assert bg_res.ok is True
        assert "job_pwsh_" in bg_res.output
        job_id = bg_res.metadata["job_id"]
        job_rec = get_job_store().get(job_id)
        assert job_rec is not None
        assert job_rec.kind == "pwsh"


# =====================================================================
# 4. Tool Registry & Permissions Integration Tests
# =====================================================================


def test_phase3_tools_registry_and_permissions(temp_workspace):
    registry = get_tool_registry()

    phase3_tools = ["code_mode", "session_query", "pwsh"]

    for tool_name in phase3_tools:
        assert registry.has_tool(tool_name) is True, f"Tool '{tool_name}' missing from registry"
        tool_def = registry.get(tool_name)
        assert tool_def is not None
        openai_schema = tool_def.to_openai_schema()
        assert openai_schema["type"] == "function"
        assert openai_schema["function"]["name"] == tool_name

    # Check permission requests
    code_mode_perm = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c1",
            "function": {"name": "code_mode", "arguments": json.dumps({"code": "x = 1"})},
        },
    )
    assert "write-in-cwd" in code_mode_perm["scopes"]

    session_query_perm = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c2",
            "function": {"name": "session_query", "arguments": json.dumps({"query": "auth"})},
        },
    )
    assert "read-in-cwd" in session_query_perm["scopes"]

    pwsh_perm = describe_tool_permission_request(
        session_id="s1",
        project_root=temp_workspace,
        tool_call={
            "id": "c3",
            "function": {
                "name": "pwsh",
                "arguments": json.dumps(
                    {"command": "Get-ChildItem", "sideEffects": ["read-in-cwd"]}
                ),
            },
        },
    )
    assert "read-in-cwd" in pwsh_perm["scopes"]
