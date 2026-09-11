"""Consolidated tool tests: canonical file/shell tools, search, str_replace, terminal, web (mocked)."""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from coderai.core.state import clear_session_state
from coderai.core.tools.ask_user_question import handle as ask_handle
from coderai.core.tools.bash import clear_session_working_dir, handle as bash_handle
from coderai.core.tools.edit import handle as edit_handle
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.read import handle as read_handle
from coderai.core.tools.registry import ToolRegistry
from coderai.core.tools.search import handle_glob_tool, handle_grep_tool, resolve_rg_path
from coderai.core.tools.str_replace_editor import handle_str_replace_editor_tool
from coderai.core.tools.terminal import (
    handle_terminal_close_tool,
    handle_terminal_open_tool,
    handle_terminal_send_tool,
)
from coderai.core.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionHooks,
    ToolResult,
)
from coderai.core.tools.update_plan import handle as plan_handle
from coderai.core.tools.write import handle as write_handle


def _ctx(tmp_path: pathlib.Path, session_id: str = "sess") -> dict:
    """Build a minimal dict tool context rooted at tmp_path."""
    clear_session_state(session_id)
    clear_session_working_dir(session_id)
    return {"session_id": session_id, "project_root": str(tmp_path)}


def test_bash_executes_command_returns_output(tmp_path):
    """Bash runs echo and reports a zero exit code with cwd metadata."""
    res = bash_handle({"command": "echo 'Hello CoderAI'"}, _ctx(tmp_path, "bash_basic"))
    assert res.ok and "Hello CoderAI" in (res.output or "")
    assert res.metadata["exitCode"] == 0
    assert res.metadata["cwd"] is not None


def test_bash_cwd_tracking_persists_across_calls(tmp_path):
    """cd updates the session cwd used by subsequent bash invocations."""
    sub = tmp_path / "sub"
    sub.mkdir()
    ctx = _ctx(tmp_path, "bash_cwd")
    assert bash_handle({"command": "cd sub"}, ctx).metadata["cwd"] == str(sub)
    assert str(sub) in (bash_handle({"command": "pwd"}, ctx).output or "")


def test_read_formats_numbered_snippets(tmp_path):
    """Read returns line-numbered output plus full and partial snippet ids."""
    p = tmp_path / "sample.txt"
    p.write_text("first\nsecond\nthird\n")
    ctx = _ctx(tmp_path, "read_fmt")
    full = read_handle({"file_path": str(p)}, ctx)
    assert full.ok and "     1\tfirst" in (full.output or "")
    assert full.metadata["snippet"]["id"].startswith("full_file_")
    part = read_handle({"file_path": str(p), "offset": 2, "limit": 1}, ctx)
    assert part.ok and "second" in (part.output or "") and "first" not in (part.output or "")


def test_read_rejects_ambiguous_relative_path(tmp_path):
    """A bare filename matching two dirs fails instead of guessing."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "dup.py").write_text("x = 1")
    (tmp_path / "b" / "dup.py").write_text("x = 2")
    res = read_handle({"file_path": "dup.py"}, _ctx(tmp_path, "read_ambig"))
    assert not res.ok and "ambiguous" in res.error


def test_write_creates_new_file_with_preview(tmp_path):
    """Write creates missing files and includes a diff preview."""
    p = tmp_path / "created.py"
    res = write_handle(
        {"file_path": str(p), "content": "print('hi')\n"}, _ctx(tmp_path, "write_new")
    )
    assert res.ok and res.output == "Created file."
    assert res.metadata["type"] == "create" and res.metadata["diff_preview"] is not None
    assert p.read_text() == "print('hi')\n"


def test_write_requires_full_read_before_overwrite(tmp_path):
    """Overwriting without a prior full read is rejected to prevent clobbering."""
    p = tmp_path / "existing.py"
    p.write_text("line 1\nline 2\n")
    ctx = _ctx(tmp_path, "write_safe")
    assert (
        "Must read the full existing file"
        in write_handle({"file_path": str(p), "content": "x"}, ctx).error
    )
    read_handle({"file_path": str(p), "offset": 1, "limit": 1}, ctx)
    assert (
        "Must read the full existing file"
        in write_handle({"file_path": str(p), "content": "x"}, ctx).error
    )
    read_handle({"file_path": str(p)}, ctx)
    assert write_handle({"file_path": str(p), "content": "line 1\nline 2\n"}, ctx).ok


def test_edit_replaces_exact_match_only(tmp_path):
    """Edit applies an exact old_string replacement and reports the match mode."""
    p = tmp_path / "target.py"
    p.write_text("def add(a, b):\n    return a - b\n")
    ctx = _ctx(tmp_path, "edit_exact")
    snip = read_handle({"file_path": str(p)}, ctx).metadata["snippet"]["id"]
    res = edit_handle(
        {
            "snippet_id": snip,
            "file_path": str(p),
            "old_string": "    return a - b",
            "new_string": "    return a + b",
        },
        ctx,
    )
    assert res.ok and res.metadata["matched_via"] == "exact"
    assert p.read_text() == "def add(a, b):\n    return a + b\n"


def test_edit_replace_all_requires_expected_count(tmp_path):
    """Bulk replacement without a correct expected count is guarded."""
    p = tmp_path / "guards.py"
    p.write_text("x = 1\nx = 1\nx = 1\n")
    ctx = _ctx(tmp_path, "edit_guards")
    snip = read_handle({"file_path": str(p)}, ctx).metadata["snippet"]["id"]
    base = {"snippet_id": snip, "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}
    assert "expected_occurrences" in edit_handle(base, ctx).error
    assert "found 3" in edit_handle({**base, "expected_occurrences": 2}, ctx).error
    ok = edit_handle({**base, "expected_occurrences": 3}, ctx)
    assert ok.ok and p.read_text() == "x = 2\nx = 2\nx = 2\n"


@pytest.mark.asyncio
async def test_ask_defers_for_multi_select(tmp_path):
    """AskUserQuestion renders multi-select options and awaits user input."""
    res = await ask_handle(
        {
            "questions": [
                {
                    "question": "Pick?",
                    "multiSelect": True,
                    "options": [{"label": "A", "description": "first"}],
                }
            ]
        },
        {"session_id": "ask", "project_root": str(tmp_path)},
    )
    assert res.ok and res.await_user_response is True
    assert "multi-select" in res.output and "- A" in res.output


def test_plan_rejects_empty_and_accepts_content(tmp_path):
    """UpdatePlan rejects blank plans and echoes back valid plan text."""
    ctx = {"session_id": "plan", "project_root": str(tmp_path)}
    assert "non-empty string" in plan_handle({"plan": ""}, ctx).error
    res = plan_handle({"plan": "1. Read\n2. Edit\n", "explanation": "start"}, ctx)
    assert res.ok and res.output == "Plan updated." and res.metadata["plan"].startswith("1. Read")


@pytest.mark.asyncio
async def test_executor_dispatches_canonical_tool_successfully(tmp_path):
    """Executor runs a canonical read call and returns its output."""
    p = tmp_path / "canon.py"
    p.write_text("print('hello')\n")
    clear_session_state("exec_canon")
    results = await ToolExecutor(project_root=str(tmp_path)).execute_tool_calls(
        "exec_canon",
        [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read", "arguments": json.dumps({"file_path": str(p)})},
            }
        ],
    )
    assert len(results) == 1 and results[0]["result"]["ok"] is True
    assert "hello" in results[0]["result"]["output"]


@pytest.mark.asyncio
async def test_executor_rejects_malformed_json_gracefully(tmp_path):
    """Malformed tool arguments surface as parse errors instead of raising."""
    result = await ToolExecutor(project_root=str(tmp_path)).execute_tool_call(
        "s", {"id": "c", "type": "function", "function": {"name": "read", "arguments": "{bad_json"}}
    )
    assert not result.ok and "InputParseError" in (result.error or "")


@pytest.mark.asyncio
async def test_executor_fails_closed_on_ask_decision(tmp_path):
    """An ask permission decision without user reply fails closed."""
    denied = await ToolExecutor(project_root=str(tmp_path)).execute_tool_call(
        "s",
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"file_path": "x"})},
        },
        hooks=ToolExecutionHooks(permission_decision="ask"),
    )
    assert denied.ok is False and "fail-closed" in (denied.error or "")


@pytest.mark.asyncio
async def test_executor_enforces_timeout_budget(tmp_path):
    """Slow handlers exceeding their timeout return TOOL_TIMEOUT."""
    registry = ToolRegistry()

    async def _slow(args, ctx):
        await asyncio.sleep(0.5)
        return ToolResult(ok=True, name="slow", output="finished")

    registry.register(
        ToolDefinition(name="slow_probe", parameters={}, required=[], handler=_slow, timeout_ms=50)
    )
    res = await ToolExecutor(project_root=str(tmp_path), registry=registry).execute_tool_call(
        "s", {"id": "t1", "type": "function", "function": {"name": "slow_probe", "arguments": "{}"}}
    )
    assert not res.ok and "TOOL_TIMEOUT" in (res.error or "")


def test_search_glob_finds_files_skips_vcs(tmp_path, monkeypatch):
    """Glob finds python files while skipping hidden VCS metadata dirs."""
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha = 1\n")
    (tmp_path / ".svn").mkdir()
    (tmp_path / ".svn" / "entries").write_text("12\n")
    ctx = ToolExecutionContext(session_id="glob_sess", project_root=str(tmp_path))
    out = handle_glob_tool({"pattern": "*.py"}, ctx).output or ""
    assert "src/a.py" in out and ".svn/entries" not in out


def test_search_grep_groups_matches_by_file(tmp_path, monkeypatch):
    """Grep reports grouped line matches with a total match count."""
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    (tmp_path / "hits.py").write_text("nothing\nfindme here\n")
    ctx = ToolExecutionContext(session_id="grep_sess", project_root=str(tmp_path))
    res = handle_grep_tool({"pattern": "findme", "include": "*.py"}, ctx)
    assert res.ok and "hits.py" in (res.output or "") and "Found 1 match" in (res.output or "")


def test_search_bundled_rg_resolves_executable():
    """Bundled ripgrep resolves to an executable file when present."""
    path = resolve_rg_path()
    if path is not None:
        assert pathlib.Path(path).is_file()


def test_str_replace_creates_views_and_replaces(tmp_path):
    """str_replace_editor creates, views, replaces, and undoes file edits."""
    ctx = ToolExecutionContext(session_id="sre_sess", project_root=str(tmp_path))
    path = str(tmp_path / "doc.txt")
    assert handle_str_replace_editor_tool(
        {"command": "create", "path": path, "file_text": "apple\nbanana\n"}, ctx
    ).ok
    view = handle_str_replace_editor_tool({"command": "view", "path": path}, ctx)
    assert view.ok and "banana" in view.output
    (tmp_path / "doc.txt").write_text("apple\nbanana\n")
    handle_str_replace_editor_tool({"command": "view", "path": path}, ctx)
    assert handle_str_replace_editor_tool(
        {"command": "str_replace", "path": path, "old_str": "banana", "new_str": "berry"}, ctx
    ).ok
    assert "berry" in pathlib.Path(path).read_text()
    assert handle_str_replace_editor_tool({"command": "undo_edit", "path": path}, ctx).ok
    assert "banana" in pathlib.Path(path).read_text()


def test_terminal_lifecycle_opens_sends_closes(tmp_path):
    """PTY terminals open, echo commands, and close cleanly."""
    ctx = ToolExecutionContext(session_id="term_sess", project_root=str(tmp_path))
    opened = handle_terminal_open_tool({"type": "sh", "name": "consolidated"}, ctx)
    assert opened.ok
    sid = opened.metadata["sessionId"]
    sent = handle_terminal_send_tool(
        {"sessionId": sid, "text": "echo 'HELLO_TERM'", "submit": True, "timeout_ms": 2000}, ctx
    )
    assert sent.ok and "HELLO_TERM" in sent.metadata["output"]
    closed = handle_terminal_close_tool({"sessionId": sid}, ctx)
    assert closed.ok and "closed successfully" in closed.output


@pytest.mark.asyncio
async def test_web_search_mocked_returns_sources(tmp_path, monkeypatch):
    """WebSearch renders mocked provider sources without network access."""
    from coderai.core.tools.web_search import handle as search_handle
    import coderai.tools.web.search as search_mod
    from coderai.core.web_providers import WebSearchResult, WebSearchSource

    class _FakeProvider:
        id = "mock"

        def search(self, query, max_results=8, timeout_seconds=15.0):
            return WebSearchResult(
                query=query,
                content="Mock summary",
                sources=[
                    WebSearchSource(
                        title="Docs", url="https://example.com/docs", snippet="mock snippet"
                    )
                ],
            )

    monkeypatch.setattr(
        search_mod, "resolve_web_search_provider", lambda name=None: _FakeProvider()
    )
    ctx = {"session_id": "ws", "project_root": str(tmp_path)}
    res = await search_handle({"query": "mock query"}, ctx)
    assert res.ok and "Mock summary" in res.output
    assert "https://example.com/docs" in res.output


@pytest.mark.asyncio
async def test_web_fetch_mocked_returns_markdown(tmp_path, monkeypatch):
    """WebFetch converts mocked HTML into markdown without network access."""
    from coderai.core.tools.web_fetch import handle as fetch_handle
    import coderai.tools.web.fetch as fetch_mod

    class _FakeResp:
        ok = True
        error = None
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><head><title>Hi</title></head><body><h1>Hello Page</h1></body></html>"
        url = "https://example.com/"
        from_cache = False
        elapsed_ms = 5

    class _FakeClient:
        async def get_async(self, url, timeout=None, use_cache=True, cache_ttl=300.0):
            return _FakeResp()

    monkeypatch.setattr(fetch_mod, "get_http_client", lambda: _FakeClient())
    res = await fetch_handle(
        {"url": "https://example.com/"}, {"session_id": "wf", "project_root": str(tmp_path)}
    )
    assert res.ok and "Hello Page" in res.output
