"""Regression tests for the 2026-09-22 Phase 1 remediation items."""

from __future__ import annotations

import asyncio
import datetime
import json
import math
import os
import pathlib
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coderai.auth.oauth import OAuthManager, OAuthToken, save_token
from coderai.config import TypedConfig, _typed_global_knobs
from coderai.schedule import ScheduleManager
from coderai.tools.file.replace import handle_edit_tool
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.registry import ToolRegistry
from coderai.tools.legacy.sanitizer import sanitize_text
from coderai.tools.legacy.types import ToolDefinition, ToolResult
from coderai.tools.todo import handle_todo_write_tool
from coderai.utils.aioqueue import Queue


def test_kill_all_tasks_returns_without_deadlock() -> None:
    """WF-A1: bulk kill must not re-enter JobStore's lock."""
    from coderai.background.agent_runner import TaskSupervisor
    from coderai.background.manager import get_job_store, reset_job_store

    reset_job_store()
    store = get_job_store()
    store.start(job_id="phase1-job", session_id="S", kind="bash", label="sleep")

    class _EmptyRegistry:
        def list(self, parent_session_id: str | None = None) -> list[object]:
            return []

    supervisor = TaskSupervisor(_EmptyRegistry())  # type: ignore[arg-type]
    box: dict[str, object] = {}

    def _run() -> None:
        box["ids"] = supervisor.kill_all_tasks("S")

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(2.0)
    try:
        assert not worker.is_alive()
        assert box["ids"] == ["phase1-job"]
        assert store._jobs["phase1-job"].status == "killed"
    finally:
        reset_job_store()


@pytest.mark.asyncio
async def test_handler_typeerror_runs_once(tmp_path: pathlib.Path) -> None:
    """TL-A1: a TypeError inside a sync handler is not a reason to run it again."""
    calls = {"n": 0}

    def _boom(_args: dict[str, object], _ctx: object) -> ToolResult:
        calls["n"] += 1
        raise TypeError("handler rejected the payload")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="typeerror_probe", parameters={}, required=[], handler=_boom)
    )
    result = await ToolExecutor(project_root=str(tmp_path), registry=registry).execute_tool_call(
        "s",
        {
            "id": "t1",
            "type": "function",
            "function": {"name": "typeerror_probe", "arguments": "{}"},
        },
    )
    assert calls["n"] == 1
    assert result.ok is False
    assert "handler rejected the payload" in (result.error or "")


@pytest.mark.asyncio
async def test_wire_server_uses_aioqueue_and_shuts_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """IN-A3: the stdio server queue shuts down without Python 3.13 Queue APIs."""
    from coderai.wire.server import WireServer, get_active_wire_server

    monkeypatch.setattr(sys.stdin, "readline", lambda: "")
    mgr = MagicMock()
    mgr.root_wire_hub = None
    server = WireServer(mgr, session_id=None)
    assert isinstance(server._write_queue, Queue)
    try:
        code = await asyncio.wait_for(server.serve(), timeout=2.0)
    finally:
        if get_active_wire_server() is server:
            # serve() clears this on the success path; don't leak it if we timed out.
            import coderai.wire.server as wire_server

            wire_server._active_wire_server = None
    assert code == 0
    assert get_active_wire_server() is None


@pytest.mark.asyncio
async def test_slash_dispatch_exception_stays_in_repl() -> None:
    """UI-A1: a handler exception is printed as plain text and does not escape."""
    from coderai.ui.shell.app import SlashAction, _guard_slash_dispatch

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom [/b] world")

    ctx = SimpleNamespace()
    recorder = SimpleNamespace(lines=[])

    def _print(msg: object, **_kwargs: object) -> None:
        recorder.lines.append(str(msg))

    recorder.print = _print  # type: ignore[attr-defined]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("coderai.ui.shell.app.dispatch_slash_command", _boom)
        action = await _guard_slash_dispatch(
            "explode",
            "",
            ctx,  # type: ignore[arg-type]
            drain_fn=None,
            mgr=MagicMock(),
            session_id=None,
            console=recorder,
        )
    assert action is SlashAction.HANDLED
    rendered = "\n".join(recorder.lines)
    assert "boom" in rendered
    assert r"\[/b]" in rendered


@pytest.mark.asyncio
async def test_slash_dispatch_keyboard_interrupt_is_handled() -> None:
    """UI-A1: Ctrl-C during a slash command continues the REPL."""
    from coderai.ui.shell.app import SlashAction, _guard_slash_dispatch

    async def _interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("coderai.ui.shell.app.dispatch_slash_command", _interrupt)
        action = await _guard_slash_dispatch(
            "effort",
            "",
            SimpleNamespace(),  # type: ignore[arg-type]
            drain_fn=None,
            mgr=MagicMock(),
            session_id="sess",
            console=None,
        )
    assert action is SlashAction.HANDLED


def test_stream_flag_does_not_queue_input_after_interrupt() -> None:
    """UI-A5: a leftover streaming flag must not queue the next prompt."""
    from coderai.ui.shell.app import _STREAM_STATE, _run_interactive, repl_input_is_streaming
    import inspect

    _STREAM_STATE.is_streaming = True
    try:
        assert repl_input_is_streaming(None) is False
    finally:
        _STREAM_STATE.reset()
    assert _STREAM_STATE.is_streaming is False
    source = inspect.getsource(_run_interactive)
    assert "repl_input_is_streaming" in source
    assert 'getattr(_STREAM_STATE, "is_streaming"' not in source


def test_arrow_picker_eof_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    """UI-A12: EOF from stdin cancels the picker instead of spinning."""
    import coderai.ui.shell.session_picker as picker

    monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(picker, "_read_single_key", lambda: "")
    box: dict[str, object] = {}

    def _run() -> None:
        box["result"] = picker.select_with_arrows(
            None, [("a", "Alpha", "first")], allow_cancel=True
        )

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(1.5)
    assert not worker.is_alive()
    assert box["result"] is None


def test_arrow_picker_live_eof_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    """UI-A12: the Rich Live picker treats EOF as cancel too."""
    import coderai.ui.shell.session_picker as picker

    class _Live:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Live:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def update(self, *_args: object, **_kwargs: object) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(picker, "_read_single_key", lambda: "")
    monkeypatch.setattr(picker, "Live", _Live)
    monkeypatch.setattr(picker, "_RICH", True)
    box: dict[str, object] = {}

    def _run() -> None:
        box["result"] = picker.select_with_arrows(
            SimpleNamespace(), [("a", "Alpha", "first")], allow_cancel=True
        )

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(1.5)
    assert not worker.is_alive()
    assert box["result"] is None


class _FakePrompt:
    def __init__(self, second: BaseException | str) -> None:
        self._second = second
        self.calls = 0

    def update_plan_mode(self, _plan_mode: bool) -> None:
        return None

    async def prompt_async(self, _message: object) -> str:
        self.calls += 1
        if self.calls == 1:
            return "echo hi \\"
        if isinstance(self._second, BaseException):
            raise self._second
        return str(self._second)


@pytest.mark.asyncio
async def test_multiline_ctrl_c_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    """UI-A11: Ctrl-C in a continuation cancels instead of submitting the buffer."""
    import coderai.ui.shell.prompt as prompt

    monkeypatch.setattr(prompt, "HAS_PTK", True)
    monkeypatch.setattr(prompt.os, "isatty", lambda _fd: True)
    session = _FakePrompt(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        await prompt.read_user_turn_ptk("❯ ", project_root="/tmp", session=session)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_multiline_eof_keeps_partial_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    """UI-A11: EOF still finishes the buffer."""
    import coderai.ui.shell.prompt as prompt

    monkeypatch.setattr(prompt, "HAS_PTK", True)
    monkeypatch.setattr(prompt.os, "isatty", lambda _fd: True)
    session = _FakePrompt(EOFError())
    text = await prompt.read_user_turn_ptk("❯ ", project_root="/tmp", session=session)  # type: ignore[arg-type]
    assert "echo hi" in text


def test_dmail_imports_on_its_own() -> None:
    """TL-A21: importing the dmail tool must not raise a circular ImportError."""
    root = pathlib.Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", "import coderai.tools.dmail"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_persistent_bash_reads_exit_code_and_cwd(tmp_path: pathlib.Path) -> None:
    """TL-A3: the persistent shell reports a real exit code and follows cd."""
    from coderai.terminal.manager import get_terminal_manager
    from coderai.tools.shell import handle_bash_tool, session_working_dirs

    session_id = "phase1-persistent"
    nested = tmp_path / "nested"
    nested.mkdir()
    ctx = {
        "session_id": session_id,
        "project_root": str(tmp_path),
        "sandbox_mode": "workspace-write",
    }
    try:
        failed = handle_bash_tool({"command": "false", "persistent": True}, ctx)
        assert failed.ok is False
        assert failed.metadata["exitCode"] == 1
        moved = handle_bash_tool({"command": f"cd {nested}", "persistent": True}, ctx)
        assert moved.ok is True
        assert pathlib.Path(moved.metadata["cwd"]).resolve() == nested.resolve()
        assert pathlib.Path(session_working_dirs[session_id]).resolve() == nested.resolve()
    finally:
        session_working_dirs.pop(session_id, None)
        term_id = f"persistent_bash_{session_id}"
        get_terminal_manager().close_session(term_id)


def test_typed_global_knobs_reads_mcp_client_timeout() -> None:
    """DC-E: typed MCP timeout lives on mcp.client, and the knobs dict is filled."""
    knobs = _typed_global_knobs(TypedConfig())
    assert knobs["mergeAllAvailableSkills"] is True
    assert knobs["extraSkillDirs"] == []
    assert knobs["notificationsClaimStaleAfterMs"] == 15_000
    assert knobs["mcpToolCallTimeoutMs"] == 60_000


def test_sanitizer_keeps_identifiers_and_redacts_real_secrets() -> None:
    """TL-A4: word-like sk- fragments and trailing emails survive; real secrets do not."""
    ident = "disk-usage-monitoring-service-config"
    assert sanitize_text(ident)[0] == ident

    key = "sk-" + ("a" * 32)
    redacted, kinds = sanitize_text(f"token={key}")
    assert key not in redacted
    assert "openai_api_key" in kinds

    uri = "postgres://user:s3cret@db.example.com/app contact ops@example.com"
    cleaned, uri_kinds = sanitize_text(uri)
    assert "s3cret" not in cleaned
    assert "ops@example.com" in cleaned
    assert "[REDACTED_DB_PASSWORD]" in cleaned
    assert "database_uri_password" in uri_kinds


def test_edit_allows_partial_fix_of_unparseable_python(tmp_path: pathlib.Path) -> None:
    """TL-A12: reject a .py edit only when it newly introduces a syntax error."""
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(\n    x = 1\ndef also(\n    y = 2\n")
    ctx = {"session_id": "phase1-edit", "project_root": str(tmp_path)}
    partial = handle_edit_tool(
        {"file_path": str(broken), "old_string": "x = 1", "new_string": "x = 2"},
        ctx,
    )
    assert partial.ok is True
    assert "x = 2" in broken.read_text()

    valid = tmp_path / "ok.py"
    valid.write_text("x = 1\n")
    rejected = handle_edit_tool(
        {"file_path": str(valid), "old_string": "x = 1", "new_string": "x = ("},
        ctx,
    )
    assert rejected.ok is False
    assert "invalid Python syntax" in (rejected.error or "")
    assert valid.read_text() == "x = 1\n"


def test_todo_write_merge_false_succeeds() -> None:
    """TL-A16: merge=False must not be passed through as the explanation."""
    result = handle_todo_write_tool(
        {
            "todos": [{"content": "fix the crash", "status": "pending"}],
            "merge": False,
        },
        {"session_id": "phase1-todo", "project_root": "."},
    )
    assert result.ok is True
    assert "explanation must be a string" not in (result.error or "")

    registry = ToolRegistry()
    params = registry.get("todo_write").parameters
    assert "merge" not in params
    assert "explanation" in params


def test_schedule_create_schema_matches_handler() -> None:
    """TL-A14: the schema advertises the selectors the handler actually reads."""
    registry = ToolRegistry()
    params = registry.get("schedule_create").parameters
    assert "cron_expression" not in params
    assert "after_seconds" in params
    assert "at" in params
    assert "every_seconds" in params


def test_schedule_target_survives_restart(tmp_path: pathlib.Path) -> None:
    """WF-A10: a reloaded schedule keeps its fire time and does not run immediately."""
    path = tmp_path / "schedule.json"
    manager = ScheduleManager(str(path))
    record = manager.create(prompt="later", after_seconds=3600, session_id="s")
    assert record.target_timestamp > time.time() + 3000

    reloaded = ScheduleManager(str(path))
    loaded = reloaded._schedules[record.id]
    assert loaded.target_timestamp > time.time() + 3000
    assert reloaded.check_due(session_id="s") == []
    assert not list(path.parent.glob("*.tmp"))

    legacy = {
        "nextId": 2,
        "schedules": [
            {
                "id": "sched_legacy",
                "prompt": "from scheduledAt",
                "kind": "at",
                "scheduledAt": (
                    datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
                )
                .isoformat()
                .replace("+00:00", "Z"),
                "createdAt": datetime.datetime.now(datetime.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "state": "scheduled",
            }
        ],
    }
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    from_legacy = ScheduleManager(str(legacy_path))
    assert from_legacy._schedules["sched_legacy"].target_timestamp > time.time() + 3000


def test_every_seconds_keeps_minimum_message() -> None:
    """WF-A10: a too-small interval keeps its validation message."""
    manager = ScheduleManager(None)
    with pytest.raises(ValueError, match="at least"):
        manager.create(prompt="soon", every_seconds=10)


@pytest.mark.asyncio
async def test_team_task_create_unknown_dependency_is_tool_error() -> None:
    """WF-A15: a missing dependency is a tool error, not a raised KeyError."""
    from coderai.teams.tools import handle_team_task_create_tool

    result = await handle_team_task_create_tool(
        {"title": "phase1", "dependencies": ["task_missing"]},
        MagicMock(),
    )
    assert result.ok is False
    assert "task_missing" in (result.error or "")


@pytest.mark.asyncio
async def test_team_task_update_bad_revision_is_tool_error() -> None:
    """WF-A15: a non-integer expected_revision stays inside the tool result."""
    from coderai.teams.tools import handle_team_task_update_tool

    result = await handle_team_task_update_tool(
        {"task_id": "task_missing", "expected_revision": "nope"},
        MagicMock(),
    )
    assert result.ok is False
    assert result.error


@pytest.mark.asyncio
async def test_wait_agent_clamps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """WF-A15: negative, infinite, and NaN timeouts stay inside 0–600 seconds."""
    from coderai.teams.manager import TeamManager
    from coderai.teams.tools import handle_wait_agent_tool

    seen: list[float] = []

    async def _fake_wait(
        self: object,
        agent_ids: object,
        timeout_seconds: float = 60.0,
        wait_for: str = "completion",
    ) -> dict[str, object]:
        del self, agent_ids, wait_for
        seen.append(timeout_seconds)
        return {"ok": True, "status": "settled", "elapsed_seconds": 0, "agents": []}

    monkeypatch.setattr(TeamManager, "wait_agent", _fake_wait)
    for raw, expected in (
        (-5, 0.0),
        (float("inf"), 60.0),
        (float("nan"), 60.0),
        (10_000, 600.0),
    ):
        result = await handle_wait_agent_tool(
            {"agent_id": "agt", "timeout_seconds": raw},
            MagicMock(),
        )
        assert result.ok is True
        assert math.isfinite(seen[-1])
        assert seen[-1] == expected


def test_effort_xhigh_is_accepted() -> None:
    """UI-A15: /effort accepts the efforts declared in config, including xhigh."""
    from coderai.ui.shell.dispatch import SlashAction, cmd_effort

    ctx = SimpleNamespace(console=None, mgr=MagicMock())
    action = cmd_effort(ctx, "xhigh")  # type: ignore[arg-type]
    assert action is SlashAction.HANDLED
    ctx.mgr.set_reasoning_effort.assert_called_once_with("xhigh")

    rejected = cmd_effort(ctx, "ludicrous")  # type: ignore[arg-type]
    assert rejected is SlashAction.HANDLED
    assert ctx.mgr.set_reasoning_effort.call_count == 1


def test_ensure_fresh_continues_past_a_rejected_key(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IN-A9: one rejected refresh token must not skip the remaining keys."""
    import coderai.auth.oauth as oauth_mod

    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path))
    oauth_mod._REJECTED_REFRESH_TOKENS.clear()
    save_token("oauth/k1", OAuthToken(access_token="old1", refresh_token="dead", expires_at=1.0))
    save_token("oauth/k2", OAuthToken(access_token="old2", refresh_token="live", expires_at=1.0))
    oauth_mod._REJECTED_REFRESH_TOKENS["oauth/k1"] = oauth_mod._RejectedRefreshState(
        refresh_token="dead",
        retry_after=time.time() + 3600,
    )
    # The pre-lock tombstone check already continues. Hide it once so the
    # in-lock rejection path (the old ``return``) is what runs for k1.
    real_tombstone = OAuthManager._tombstone
    seen = {"k1": 0}

    def _tombstone(self: OAuthManager, key: str, refresh_token: str | None):
        if key == "oauth/k1":
            seen["k1"] += 1
            if seen["k1"] == 1:
                return None
        return real_tombstone(self, key, refresh_token)

    monkeypatch.setattr(OAuthManager, "_tombstone", _tombstone)
    fresh = OAuthToken(access_token="new2", refresh_token="live2", expires_at=9_999_999_999.0)
    monkeypatch.setattr(oauth_mod, "refresh_access_token", lambda rt, **_k: fresh)
    manager = OAuthManager(["oauth/k1", "oauth/k2"])
    try:
        asyncio.run(manager.ensure_fresh())
        assert manager.get_cached_access_token("oauth/k1") is None
        assert manager.get_cached_access_token("oauth/k2") == "new2"
    finally:
        oauth_mod._REJECTED_REFRESH_TOKENS.pop("oauth/k1", None)


def test_doctor_reports_failed_mcp_without_creating_dotdir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IN-A11: failed and unauthorized MCP servers are warnings, and .coderai/ is not created."""
    from coderai.cli.doctor import run_doctor_diagnostics

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))
    project = tmp_path / "project"
    project.mkdir()
    mgr = SimpleNamespace(
        mcp_manager=SimpleNamespace(
            server_statuses=[
                SimpleNamespace(name="alpha", status="failed"),
                SimpleNamespace(name="beta", status="unauthorized"),
            ],
            clients={},
        ),
        mcp_tool_definitions=[],
        get_active_model=lambda: "test-model",
        get_resolved_settings=lambda: {},
    )
    report = run_doctor_diagnostics(str(project), mgr)
    mcp = next(item for item in report.items if item.name == "MCP Servers")
    assert mcp.status == "warn"
    assert "alpha" in mcp.message and "beta" in mcp.message
    assert not (project / ".coderai").exists()
