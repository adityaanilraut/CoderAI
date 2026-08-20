"""Phase 1 execution-loop contracts: pipeline, LLM retry, jobs, repeat reminders."""

from __future__ import annotations

import json
from typing import Any

import pytest

from coderai.core.common.llm_retry import (
    classify_llm_failure,
    is_empty_llm_response,
    retry_delay_ms,
)
from coderai.core.common.repeat_tool_reminder import (
    GENTLE_REMINDER,
    RepeatToolReminder,
    canonicalize_arguments,
)
from coderai.core.jobs import get_job_store, reset_job_store
from coderai.core.prompt import TOOL_DOCS, get_tools
from coderai.core.session import SessionManager
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.registry import ToolRegistry, get_tool_registry
from coderai.core.tools.types import ToolDefinition, ToolExecutionHooks, ToolResult


def test_classify_llm_failure_retryable_codes() -> None:
    class RateLimit(Exception):
        status_code = 429

    class Server(Exception):
        status_code = 503

    class Timeout(Exception):
        def __str__(self) -> str:
            return "Request timed out"

    class Transport(Exception):
        def __str__(self) -> str:
            return "Connection error"

    class Auth(Exception):
        status_code = 401

    assert classify_llm_failure(RateLimit()) == "RATE_LIMIT"
    assert classify_llm_failure(Server()) == "SERVER"
    assert classify_llm_failure(Timeout()) == "TIMEOUT"
    assert classify_llm_failure(Transport()) == "TRANSPORT"
    assert classify_llm_failure(Auth()) is None


def test_is_empty_llm_response() -> None:
    assert is_empty_llm_response({"choices": [{"message": {"content": ""}}]}) is True
    assert is_empty_llm_response({"choices": [{"message": {"content": "hi"}}]}) is False
    assert (
        is_empty_llm_response(
            {"choices": [{"message": {"content": "", "tool_calls": [{"id": "1"}]}}]}
        )
        is False
    )


def test_retry_delay_ms_exponential_without_jitter() -> None:
    delay = retry_delay_ms(1, random_fn=lambda: 0.5)
    assert delay == 500
    delay2 = retry_delay_ms(2, random_fn=lambda: 0.5)
    assert delay2 == 1000
    delay_cap = retry_delay_ms(20, random_fn=lambda: 0.5)
    assert delay_cap == 10_000


def test_repeat_tool_reminder_thresholds_and_exclude() -> None:
    reminder = RepeatToolReminder()
    args = {"command": "ls", "sideEffects": ["read-in-cwd"]}
    texts = [reminder.observe("bash", args) for _ in range(8)]
    assert texts[0] is None
    assert texts[1] is None
    assert texts[2] == GENTLE_REMINDER
    assert texts[3] is None
    assert texts[4] is not None and "consecutive_calls: 5" in texts[4]
    assert texts[5] is None
    assert texts[6] is None
    assert texts[7] is not None and "consecutive_calls: 8" in texts[7]
    assert reminder.observe("UpdatePlan", {"plan": "- [ ] x"}) is None
    assert canonicalize_arguments({"b": 1, "a": 2}) == canonicalize_arguments({"a": 2, "b": 1})


@pytest.mark.asyncio
async def test_executor_fail_closed_ask_and_pre_execute_guard(tmp_path) -> None:
    executor = ToolExecutor(project_root=str(tmp_path))
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "read", "arguments": json.dumps({"file_path": str(tmp_path / "x")})},
    }
    denied = await executor.execute_tool_call(
        "s", call, hooks=ToolExecutionHooks(permission_decision="ask")
    )
    assert denied.ok is False
    assert "fail-closed" in (denied.error or "")

    blocked = await executor.execute_tool_call(
        "s",
        call,
        hooks=ToolExecutionHooks(pre_execute=lambda *_a: "deny"),
    )
    assert blocked.ok is False
    assert "PreExecuteDenied" in (blocked.error or "")

    guarded = await executor.execute_tool_call(
        "s",
        call,
        hooks=ToolExecutionHooks(guards=[lambda *_a: "deny"]),
    )
    assert guarded.ok is False
    assert "GuardDenied" in (guarded.error or "")


@pytest.mark.asyncio
async def test_executor_post_execute_and_timeout(tmp_path) -> None:
    registry = ToolRegistry()

    async def sleepy(_args: dict[str, Any], _ctx: Any) -> ToolResult:
        import asyncio

        await asyncio.sleep(0.2)
        return ToolResult(ok=True, name="sleepy", output="done")

    registry.register(ToolDefinition(name="sleepy", parameters={}, required=[], handler=sleepy))
    executor = ToolExecutor(project_root=str(tmp_path), registry=registry)
    timed_out = await executor.execute_tool_call(
        "s",
        {"id": "c", "type": "function", "function": {"name": "sleepy", "arguments": "{}"}},
        hooks=ToolExecutionHooks(timeout_ms=20),
    )
    assert timed_out.ok is False
    assert "TOOL_TIMEOUT" in (timed_out.error or "")

    def post(name: str, args: dict[str, Any], result: ToolResult, _ctx: Any) -> ToolResult:
        del name, args
        result.output = f"wrapped:{result.output}"
        return result

    registry.register(
        ToolDefinition(
            name="echo",
            parameters={"v": {"type": "string"}},
            required=["v"],
            handler=lambda a, _c: ToolResult(ok=True, name="echo", output=a["v"]),
        )
    )
    echoed = await executor.execute_tool_call(
        "s",
        {
            "id": "c2",
            "type": "function",
            "function": {"name": "echo", "arguments": json.dumps({"v": "hi"})},
        },
        hooks=ToolExecutionHooks(post_execute=post),
    )
    assert echoed.ok is True
    assert echoed.output == "wrapped:hi"


@pytest.mark.asyncio
async def test_job_list_output_kill_lifecycle(tmp_path) -> None:
    reset_job_store()
    store = get_job_store()
    log = tmp_path / "job.log"
    log.write_text("hello\n")
    store.start(
        job_id="bash-1",
        session_id="sess",
        kind="bash",
        label="echo hello",
        output_path=str(log),
    )
    from coderai.core.tools.jobs import (
        handle_job_kill_tool,
        handle_job_list_tool,
        handle_job_output_tool,
    )

    ctx = type("Ctx", (), {"session_id": "sess"})()
    listed = await handle_job_list_tool({}, ctx)
    assert listed.ok is True
    assert "bash-1" in (listed.output or "")

    out1 = await handle_job_output_tool({"job_id": "bash-1"}, ctx)
    assert "hello" in (out1.output or "")
    out2 = await handle_job_output_tool({"job_id": "bash-1"}, ctx)
    assert "(no new output)" in (out2.output or "")

    store.complete("bash-1", ok=True, exit_code=0)
    killed = await handle_job_kill_tool({"job_id": "bash-1"}, ctx)
    assert killed.ok is True
    assert "already finished" in (killed.output or "")


@pytest.mark.asyncio
async def test_llm_retry_as_new_turn_does_not_persist_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("coderai.core.session.retry_delay_ms", lambda *_a, **_k: 0)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    calls = {"n": 0}

    class RateLimit(Exception):
        status_code = 429

        def __str__(self) -> str:
            return "Rate limit exceeded"

    def script(kwargs: dict[str, Any]) -> dict[str, Any]:
        msgs = kwargs.get("messages", [])
        if any("skillNames" in str(m.get("content", "")) for m in msgs):
            return {
                "choices": [{"message": {"content": '{"skillNames": []}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimit()
        return {
            "choices": [{"message": {"content": "recovered", "tool_calls": None}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    class Completions:
        def create(self, **kwargs):
            return script(kwargs)

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": Client(),
            "model": "gpt-4o",
            "thinkingEnabled": False,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "gpt-4o",
            "permissions": {
                "allow": ["read-in-cwd"],
                "deny": [],
                "ask": [],
                "defaultMode": "allowAll",
            },
        },
    )
    sid = await mgr.create_session("hello", skills=[])
    messages = mgr.list_session_messages(sid)
    contents = [m.content for m in messages if m.role == "assistant"]
    assert any("recovered" in (c or "") for c in contents)
    assert not any("Request failed" in (c or "") for c in contents)
    assert calls["n"] == 3


def test_job_tools_in_catalog() -> None:
    names = {t["function"]["name"] for t in get_tools()}
    assert {"job_list", "job_output", "job_kill"} <= names
    assert "## job_list" in TOOL_DOCS
    assert get_tool_registry().has_tool("job_list")
