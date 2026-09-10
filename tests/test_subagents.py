"""Consolidated subagent execution-engine tests.

Covers spawn/completion lifecycle, read-only sandboxing, depth limits,
parallel/timeout/cancel behavior, Task-tool dispatch, ACP parsing, external
drivers (Claude/Codex), spawn caps + settlement, forking, and env scrubbing.
All LLM/network access is mocked.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from unittest.mock import MagicMock, patch

import pytest
from types import SimpleNamespace as NS

from coderai.core.acp.protocol import AcpMessage, AcpNdjsonParser
from coderai.core.agents import get_task_supervisor, spawn_background_agent
from coderai.core.common.shell_utils import (
    build_shell_env,
    is_sensitive_env_var,
    scrub_subprocess_env,
)
from coderai.core.orchestration import (
    DEFAULT_MAX_CONTINUABLE_AGENTS,
    DEFAULT_MAX_RUNNING_JOBS,
    resolve_max_continuable_agents,
    resolve_max_running_jobs,
    settlement_summary,
)
from coderai.core.subagent import MAX_SUBAGENT_DEPTH, SubAgentManager, SubAgentResult, SubAgentSpec
from coderai.core.subagent_backends.claude_code import ClaudeCodeConfig, ClaudeCodeDriver
from coderai.core.subagent_backends.codex import CodexConfig, CodexDriver
from coderai.core.tools.agents import handle_list_agents_tool
from coderai.core.tools.bash import handle_bash_tool
from coderai.core.tools.subagent import handle_subagent_tool
from coderai.core.tools.types import ToolExecutionContext


def _msg(content, tool_calls=None):
    """Build a stub OpenAI message."""
    return NS(content=content, tool_calls=tool_calls, reasoning_content=None, refusal=None)


def _resp(message):
    """Wrap a stub message in a stub completion response."""
    usage = NS(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return NS(choices=[NS(message=message)], usage=usage)


def _tc(cid, name, args):
    """Build a minimal tool-call stub."""
    return NS(id=cid, function=NS(name=name, arguments=json.dumps(args)))


def _mock_client(responses):
    """Return a stub OpenAI client replaying canned responses in order."""
    idx = 0

    def create(**kwargs):
        nonlocal idx
        res = responses[idx] if idx < len(responses) else _resp(_msg("Default conclusion."))
        idx += 1
        return res

    return NS(chat=NS(completions=NS(create=create)))


def _manager(tmp_path, responses):
    """Build a SubAgentManager backed by a canned-response mock client."""
    return SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": _mock_client(responses), "model": "gpt-4o"},
    )


class _SlowClient:
    """Stub OpenAI client blocking inside completions.create for delay seconds."""

    def __init__(self, delay: float):
        self.chat = NS(completions=self)
        self._delay = delay

    def create(self, **kwargs):
        time.sleep(self._delay)
        return _resp(_msg("done"))


def _subprocess_result(payload: dict):
    """Build a fake successful subprocess result emitting payload as JSON stdout."""
    return NS(returncode=0, stdout=json.dumps(payload), stderr="")


async def test_subagent_spec_result_formats_markdown():
    """Spec fields persist and completed results render markdown with artifacts."""
    spec = SubAgentSpec(
        description="Search repo",
        prompt="Find auth handlers",
        mode="read_only",
        timeout_seconds=30.0,
        provider="in_process",
    )
    assert (spec.description, spec.mode, spec.timeout_seconds) == ("Search repo", "read_only", 30.0)
    res = SubAgentResult(
        task_id="task_123",
        session_id="sub_root_task_123",
        status="completed",
        summary="Found 3 auth files.",
        artifacts=["login.py"],
    )
    md = res.format_markdown()
    assert "### Sub-Agent Task Result [task_123] — ✅ COMPLETED" in md
    assert "- `login.py`" in md and res.to_dict()["status"] == "completed"


async def test_subagent_spawn_reads_file_completes(tmp_path: pathlib.Path):
    """Spawned subagent reads a workspace file and completes with its content."""
    (tmp_path / "main.py").write_text("print('hello world')\n")
    fp = str(tmp_path / "main.py")
    manager = _manager(
        tmp_path,
        [
            _resp(_msg("", [_tc("call_1", "read", {"file_path": fp})])),
            _resp(_msg("main.py contains a hello world print statement.")),
        ],
    )
    result = await manager.spawn_subagent(
        SubAgentSpec(description="Inspect main", prompt="What does main.py do?")
    )
    assert result.status == "completed" and "hello world" in result.summary
    assert (result.iterations, result.tool_calls_count) == (2, 1)
    assert any("main.py" in art for art in result.artifacts)


async def test_subagent_read_only_sandbox_blocks_write(tmp_path: pathlib.Path):
    """Read-only subagent cannot write files to disk."""
    args = {"file_path": str(tmp_path / "hack.py"), "content": "malicious"}
    manager = _manager(
        tmp_path,
        [
            _resp(_msg("", [_tc("w1", "write", args)])),
            _resp(_msg("Could not write file due to read_only sandbox.")),
        ],
    )
    result = await manager.spawn_subagent(
        SubAgentSpec(description="Attempt write", prompt="Write a file", mode="read_only")
    )
    assert result.status == "completed" and not (tmp_path / "hack.py").exists()


async def test_subagent_spawn_rejects_excess_depth(tmp_path: pathlib.Path):
    """Spawn fails when spec depth exceeds the global nesting limit."""
    result = await _manager(tmp_path, []).spawn_subagent(
        SubAgentSpec(description="Nested", prompt="Spawn a child", depth=MAX_SUBAGENT_DEPTH + 1)
    )
    assert result.status == "failed" and "nesting depth exceeded" in result.summary.lower()


async def test_subagent_spawn_rejects_depth_over_max_depth(tmp_path: pathlib.Path):
    """Spawn fails with RecursionLimitError when depth passes the spec max_depth."""
    manager = SubAgentManager(str(tmp_path), create_openai_client=lambda: {"client": None})
    result = await manager.spawn_subagent(
        SubAgentSpec(description="Nested agent", prompt="Do work", depth=3, max_depth=3)
    )
    assert result.status == "failed" and "RecursionLimitError" in (result.error or "")


async def test_subagent_spawn_halts_on_token_budget(tmp_path: pathlib.Path):
    """Spawn stops with budget_exceeded once the token budget is consumed."""
    import coderai.core.subagent as subagent_mod

    def mock_call_llm_sync(client, request):
        return {
            "choices": [
                {
                    "message": {
                        "content": "Running...",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "read_test", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 600, "completion_tokens": 500, "total_tokens": 1100},
        }

    manager = SubAgentManager(
        str(tmp_path), create_openai_client=lambda: {"client": object(), "model": "test-model"}
    )
    original, subagent_mod._call_llm_sync = subagent_mod._call_llm_sync, mock_call_llm_sync
    try:
        result = await manager.spawn_subagent(
            SubAgentSpec(description="Budgeted task", prompt="Analyze", token_budget=1000)
        )
    finally:
        subagent_mod._call_llm_sync = original
    assert result.status == "budget_exceeded" and result.total_tokens >= 1000


async def test_subagent_parallel_runs_all_specs(tmp_path: pathlib.Path):
    """Parallel fan-out completes every spec and preserves task ids."""
    factory = lambda: {"client": _mock_client([_resp(_msg("Analyzed file."))]), "model": "gpt-4o"}  # noqa: E731
    manager = SubAgentManager(project_root=str(tmp_path), create_openai_client=factory)
    specs = [
        SubAgentSpec(description=f"Analyze {t}", prompt=f"Analyze {t}", task_id=t)
        for t in ("t_a", "t_b", "t_c")
    ]
    results = await manager.run_parallel_subagents(specs, max_concurrency=2)
    assert len(results) == 3 and all(r.status == "completed" for r in results)
    assert {r.task_id for r in results} == {"t_a", "t_b", "t_c"}


async def test_subagent_spawn_times_out_slow_llm(tmp_path: pathlib.Path):
    """Spawn reports timeout when the LLM call exceeds the spec deadline."""
    manager = SubAgentManager(
        str(tmp_path), create_openai_client=lambda: {"client": _SlowClient(0.3), "model": "gpt-4o"}
    )
    result = await manager.spawn_subagent(
        SubAgentSpec(description="Slow task", prompt="Do it", timeout_seconds=0.1)
    )
    assert result.status == "timeout" and "timed out" in result.summary.lower()


async def test_subagent_cancel_all_and_unknown_safe(tmp_path: pathlib.Path):
    """Cancel-all and cancelling an unknown session do not raise."""
    manager = _manager(tmp_path, [])
    manager.cancel_all()
    manager.cancel_subagent("nonexistent_session")


async def test_subagent_cross_manager_cancel_aborts_run(tmp_path: pathlib.Path):
    """Cancelling via one manager instance aborts a run owned by another."""
    slow = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": _SlowClient(1.0), "model": "gpt-4o"},
    )
    spec = SubAgentSpec(
        description="Slow worker",
        prompt="Execute slowly",
        task_id="slow1234",
        parent_session_id="parent_sess",
    )
    task = asyncio.create_task(slow.spawn_subagent(spec))
    await asyncio.sleep(0.05)
    _manager(tmp_path, []).cancel_subagent(f"sub_{spec.parent_session_id[:8]}_{spec.task_id}")
    assert (await asyncio.wait_for(task, timeout=5.0)).status in ("interrupted", "failed")


async def test_subagent_task_tool_validates_and_executes(tmp_path: pathlib.Path):
    """Task tool rejects missing args and returns the completed summary otherwise."""
    client = _mock_client([_resp(_msg("Task completed successfully."))])
    ctx = ToolExecutionContext(
        session_id="parent_123",
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": client, "model": "gpt-4o"},
    )
    res_no_desc = await handle_subagent_tool({"prompt": "Do work"}, ctx)
    assert not res_no_desc.ok and "description" in res_no_desc.error.lower()
    res_no_prompt = await handle_subagent_tool({"description": "Work"}, ctx)
    assert not res_no_prompt.ok and "prompt" in res_no_prompt.error.lower()
    res = await handle_subagent_tool(
        {"description": "Search code", "prompt": "Find functions", "mode": "read_only"}, ctx
    )
    assert res.ok and res.name == "Task" and res.metadata["status"] == "completed"
    assert "Task completed successfully" in (res.output or "")


def test_subagent_acp_parser_decodes_ndjson():
    """ACP NDJSON parser decodes concatenated request/response frames."""
    parser = AcpNdjsonParser()
    m1 = AcpMessage(id=1, method="initialize", params={"version": "0.25.1"})
    m2 = AcpMessage(id=2, result={"sessionId": "sess_123"})
    parsed = parser.feed(m1.encode_ndjson() + m2.encode_ndjson())
    assert len(parsed) == 2
    assert parsed[0].method == "initialize" and parsed[0].params["version"] == "0.25.1"
    assert parsed[1].result["sessionId"] == "sess_123"


async def test_subagent_claude_driver_config_and_execution(monkeypatch: pytest.MonkeyPatch):
    """Claude driver keeps its config and surfaces subprocess JSON as completed."""
    import subprocess

    driver = ClaudeCodeDriver(
        ClaudeCodeConfig(
            permission_mode="acceptEdits", timeout_seconds=60.0, claude_bin="/fake/bin/claude"
        )
    )
    assert driver.config.permission_mode == "acceptEdits"
    fake = lambda *a, **k: _subprocess_result({"result": "Found 3 files"})  # noqa: E731
    monkeypatch.setattr(subprocess, "run", fake)
    res = await ClaudeCodeDriver(ClaudeCodeConfig(claude_bin="claude")).execute("Find todos")
    assert res["ok"] is True and res["status"] == "completed" and "Found 3 files" in res["summary"]


async def test_subagent_codex_driver_config_and_execution(monkeypatch: pytest.MonkeyPatch):
    """Codex driver keeps its config and surfaces subprocess JSON as completed."""
    import subprocess

    driver = CodexDriver(
        CodexConfig(
            approval_policy="approve-for-me", timeout_seconds=90.0, codex_bin="/fake/bin/codex"
        )
    )
    assert driver.config.approval_policy == "approve-for-me"
    fake = lambda *a, **k: _subprocess_result({"output": "Refactored module"})  # noqa: E731
    monkeypatch.setattr(subprocess, "run", fake)
    res = await CodexDriver(CodexConfig(codex_bin="codex")).execute("Refactor module")
    assert res["ok"] is True and res["status"] == "completed"
    assert "Refactored module" in res["summary"]


def test_subagent_spawn_cap_defaults_and_env_overrides(monkeypatch: pytest.MonkeyPatch):
    """Spawn-cap resolvers honor defaults, settings, then env overrides."""
    for var in (
        "CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION",
        "MAX_CONTINUABLE_AGENTS_PER_SESSION",
        "CODERAI_MAX_RUNNING_JOBS_PER_SESSION",
        "MAX_RUNNING_JOBS_PER_SESSION",
    ):
        monkeypatch.delenv(var, raising=False)
    assert resolve_max_continuable_agents() == DEFAULT_MAX_CONTINUABLE_AGENTS
    assert resolve_max_running_jobs() == DEFAULT_MAX_RUNNING_JOBS
    assert resolve_max_continuable_agents({"orchestration": {"maxContinuableAgents": 75}}) == 75
    assert resolve_max_running_jobs({"orchestration": {"maxRunningJobs": 60}}) == 60
    monkeypatch.setenv("CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION", "35")
    assert resolve_max_continuable_agents() == 35
    monkeypatch.setenv("CODERAI_MAX_RUNNING_JOBS_PER_SESSION", "45")
    assert resolve_max_running_jobs() == 45


def test_subagent_settlement_summary_includes_outcome():
    """Settlement summary appends the child outcome only when provided."""
    assert "Outcome:" not in settlement_summary("agent_123", "completed")
    summary = settlement_summary("agent_123", "completed", outcome="Found Aardvark.")
    assert "Background subagent agent_123 finished" in summary
    assert "Outcome:\nFound Aardvark." in summary


async def test_subagent_spawn_cap_rejects_over_limit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Background spawn raises a descriptive error once the live-agent cap is hit."""
    monkeypatch.setenv("CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION", "3")
    manager = SubAgentManager(str(tmp_path), create_openai_client=lambda: MagicMock())
    block_event = asyncio.Event()

    async def mock_run_blocking(spec: SubAgentSpec, sid: str) -> SubAgentResult:
        await block_event.wait()
        return SubAgentResult(task_id=spec.task_id, session_id=sid, status="completed")

    with patch.object(manager, "run_continuable", side_effect=mock_run_blocking):
        handles = [
            await spawn_background_agent(
                manager,
                SubAgentSpec(
                    description=f"Agent {i}", prompt=f"Do work {i}", parent_session_id="cap_session"
                ),
            )
            for i in range(3)
        ]
        with pytest.raises(RuntimeError, match="subagent spawn cap reached: at most 3"):
            await spawn_background_agent(
                manager,
                SubAgentSpec(
                    description="Agent 4", prompt="Do work 4", parent_session_id="cap_session"
                ),
            )
        block_event.set()
        await asyncio.gather(*(h.task for h in handles if h.task))


async def test_subagent_list_agents_shows_result_summary(tmp_path: pathlib.Path):
    """list_agents tool and supervisor expose the completed result summary."""
    session_id = "test_session_list_results"
    ctx = ToolExecutionContext(
        session_id=session_id, project_root=str(tmp_path), create_openai_client=lambda: MagicMock()
    )
    manager = SubAgentManager(str(tmp_path), create_openai_client=lambda: MagicMock())

    async def mock_run_continuable(spec: SubAgentSpec, sid: str) -> SubAgentResult:
        return SubAgentResult(
            task_id=spec.task_id,
            session_id=sid,
            status="completed",
            summary="Found Alligator and Aloe Vera.",
        )

    with patch.object(manager, "run_continuable", side_effect=mock_run_continuable):
        handle = await spawn_background_agent(
            manager,
            SubAgentSpec(
                description="Find A names",
                prompt="Find animal and plant starting with A",
                parent_session_id=session_id,
            ),
        )
        if handle.task:
            await handle.task
        result = await handle_list_agents_tool({}, ctx)
        assert result.ok is True and "Result: Found Alligator and Aloe Vera." in result.output
        assert handle.to_public_dict()["summary"] == "Found Alligator and Aloe Vera."
        assert (
            get_task_supervisor().get_task(handle.id)["summary"] == "Found Alligator and Aloe Vera."
        )


async def test_subagent_fork_seeds_parent_messages(tmp_path: pathlib.Path):
    """Forked subagent receives parent seed messages ahead of its own prompt."""
    received: list = []

    class MockCompletions:
        def create(self, **kwargs):
            received.extend(kwargs.get("messages", []))
            return _resp(_msg("Fork task finished."))

    def factory():
        return {"client": NS(chat=NS(completions=MockCompletions())), "model": "gpt-5.6-luna"}

    manager = SubAgentManager(str(tmp_path), create_openai_client=factory)
    seed = [
        {"role": "user", "content": "Initial user request in parent"},
        {"role": "assistant", "content": "Parent analysis and plan"},
        {"role": "user", "content": "Follow up question in parent"},
    ]
    result = await manager.spawn_subagent(
        SubAgentSpec(
            description="Verify seeded history", prompt="Execute child subtask", seed_messages=seed
        )
    )
    assert result.status == "completed" and received[0]["role"] == "system"
    assert [m["content"] for m in received[1:4]] == [s["content"] for s in seed]
    assert "Execute child subtask" in received[-1]["content"]


def test_subagent_env_scrub_protects_secrets():
    """Env helpers flag secrets, scrub subprocess envs, and sanitize shell envs."""
    assert is_sensitive_env_var("OPENAI_API_KEY") is True
    assert is_sensitive_env_var("GITHUB_TOKEN") is True
    assert is_sensitive_env_var("PATH") is False
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/test",
        "OPENAI_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "ghp_secret",
        "CUSTOM_APP_ENV": "production",
    }
    cleaned = scrub_subprocess_env(env)
    assert "OPENAI_API_KEY" not in cleaned and "GITHUB_TOKEN" not in cleaned
    assert cleaned["CUSTOM_APP_ENV"] == "production"
    preserved = scrub_subprocess_env(env, preserve_keys={"OPENAI_API_KEY"})
    assert preserved["OPENAI_API_KEY"] == "sk-secret" and "GITHUB_TOKEN" not in preserved
    os.environ["CODERAI_TEST_SECRET_KEY"] = "super-secret"
    try:
        shell_env = build_shell_env(shell_path="/bin/bash")
        assert "CODERAI_TEST_SECRET_KEY" not in shell_env
        assert shell_env.get("NO_COLOR") == "1" and shell_env.get("PAGER") == "cat"
    finally:
        os.environ.pop("CODERAI_TEST_SECRET_KEY", None)


def test_subagent_persistent_bash_retains_state(mock_tool_context):
    """Persistent bash keeps exported vars visible to later commands in-session."""
    import sys

    if sys.platform == "win32":
        pytest.skip("Persistent PTY test is POSIX only")
    ctx = mock_tool_context
    ctx.session_id = f"test_pty_{os.getpid()}"
    res1 = handle_bash_tool(
        {"command": "export PERSISTENT_VAR='coderai_persistent_success'", "persistent": True}, ctx
    )
    assert res1.ok is True and res1.metadata.get("persistent") is True
    res2 = handle_bash_tool({"command": "echo $PERSISTENT_VAR", "persistent": True}, ctx)
    assert res2.ok is True and "coderai_persistent_success" in res2.output
