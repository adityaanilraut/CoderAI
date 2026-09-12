"""Lifecycle Hooks Framework

Provides full event-driven lifecycle interception:
- Points: PreToolUse, PostToolUse, PreStep, PostStep, PrePrompt, PostPrompt, StopCriteria, SessionStart, SessionEnd.
- Pattern Matcher: exact, glob, regex, comma-delimited lists.
- Merge Precedence: 'deny' > 'ask' > 'allow' > 'none'.
- Actionability: Context injection (additionalContext), system messages, and halt triggers (continue: false).
- Environment & Payload: JSON stdin payload with subprocess execution and timeout controls.
"""

from __future__ import annotations

import asyncio
import enum
import fnmatch
import json
import logging
import os
import pathlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from coderai.tools.legacy.types import ToolExecutionContext

logger = logging.getLogger(__name__)

DEFAULT_HOOK_TIMEOUT_SECONDS = 10.0
from coderai.hooks.config import MergedHookOutcome
from coderai.hooks.engine import run_hook_point, run_hook_point_async
from coderai.hooks.events import HookPoint
def run_pre_tool_use(
    tool_name: str,
    args: dict[str, Any],
    context: ToolExecutionContext,
    settings: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
) -> str:
    """Backward-compatible helper returning 'allow' or 'deny'."""
    payload = {
        "tool_name": tool_name,
        "tool_input": args,
        "session_id": getattr(context, "session_id", "default"),
    }
    outcome = run_hook_point(
        HookPoint.PRE_TOOL_USE,
        payload=payload,
        project_root=getattr(context, "project_root", "."),
        settings=settings,
        timeout_s=timeout_s,
    )
    return "deny" if outcome.decision == "deny" else "allow"


def run_post_tool_use(
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    context: ToolExecutionContext,
    settings: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
) -> MergedHookOutcome:
    """Execute PostToolUse / post_tool_call hook point."""
    payload = {
        "tool_name": tool_name,
        "tool_input": args,
        "tool_result": result.to_dict() if hasattr(result, "to_dict") else str(result),
        "session_id": getattr(context, "session_id", "default"),
    }
    return run_hook_point(
        HookPoint.POST_TOOL_USE,
        payload=payload,
        project_root=getattr(context, "project_root", "."),
        settings=settings,
        timeout_s=timeout_s,
    )


def run_on_tool_error(
    tool_name: str,
    args: dict[str, Any],
    error: str | Exception,
    context: ToolExecutionContext,
    settings: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
) -> MergedHookOutcome:
    """Execute ToolError / on_tool_error hook point."""
    payload = {
        "tool_name": tool_name,
        "tool_input": args,
        "error": str(error),
        "session_id": getattr(context, "session_id", "default"),
    }
    return run_hook_point(
        HookPoint.ON_TOOL_ERROR,
        payload=payload,
        project_root=getattr(context, "project_root", "."),
        settings=settings,
        timeout_s=timeout_s,
    )


def run_pre_turn(
    turn: int,
    session_id: str,
    project_root: str,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute PreTurn hook point."""
    return run_hook_point(
        HookPoint.PRE_TURN,
        payload={"turn": turn, "session_id": session_id},
        project_root=project_root,
        settings=settings,
    )


def run_post_turn(
    turn: int,
    session_id: str,
    project_root: str,
    reason: str,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute PostTurn hook point."""
    return run_hook_point(
        HookPoint.POST_TURN,
        payload={"turn": turn, "session_id": session_id, "reason": reason},
        project_root=project_root,
        settings=settings,
    )


def run_on_subagent_spawn(
    parent_session_id: str,
    subagent_id: str,
    task: str,
    mode: str = "read_only",
    project_root: str = ".",
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute SubagentSpawn / on_subagent_spawn hook point."""
    return run_hook_point(
        HookPoint.ON_SUBAGENT_SPAWN,
        payload={
            "parent_session_id": parent_session_id,
            "subagent_id": subagent_id,
            "task": task,
            "mode": mode,
        },
        project_root=project_root,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Event payload builders
# ---------------------------------------------------------------------------

def _event_base(event: str, session_id: str, cwd: str) -> dict[str, Any]:
    return {"hook_event_name": event, "session_id": session_id, "cwd": cwd}


def build_pre_tool_use_payload(
    *,
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_call_id: str = "",
) -> dict[str, Any]:
    return {
        **_event_base("PreToolUse", session_id, cwd),
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_call_id": tool_call_id,
    }


def build_post_tool_use_payload(
    *,
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: str = "",
    tool_call_id: str = "",
) -> dict[str, Any]:
    return {
        **_event_base("PostToolUse", session_id, cwd),
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_output": tool_output,
        "tool_call_id": tool_call_id,
    }


def build_post_tool_use_failure_payload(
    *,
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: dict[str, Any],
    error: str,
    tool_call_id: str = "",
) -> dict[str, Any]:
    return {
        **_event_base("PostToolUseFailure", session_id, cwd),
        "tool_name": tool_name,
        "tool_input": tool_input,
        "error": error,
        "tool_call_id": tool_call_id,
    }


def build_user_prompt_submit_payload(
    *, session_id: str, cwd: str, prompt: str
) -> dict[str, Any]:
    return {**_event_base("UserPromptSubmit", session_id, cwd), "prompt": prompt}


def build_stop_payload(
    *, session_id: str, cwd: str, stop_hook_active: bool = False
) -> dict[str, Any]:
    return {
        **_event_base("Stop", session_id, cwd),
        "stop_hook_active": stop_hook_active,
    }


def build_stop_failure_payload(
    *, session_id: str, cwd: str, error_type: str, error_message: str
) -> dict[str, Any]:
    return {
        **_event_base("StopFailure", session_id, cwd),
        "error_type": error_type,
        "error_message": error_message,
    }


def build_session_start_payload(
    *, session_id: str, cwd: str, source: str
) -> dict[str, Any]:
    return {**_event_base("SessionStart", session_id, cwd), "source": source}


def build_session_end_payload(
    *, session_id: str, cwd: str, reason: str
) -> dict[str, Any]:
    return {**_event_base("SessionEnd", session_id, cwd), "reason": reason}


def build_subagent_start_payload(
    *, session_id: str, cwd: str, agent_name: str, prompt: str
) -> dict[str, Any]:
    return {
        **_event_base("SubagentStart", session_id, cwd),
        "agent_name": agent_name,
        "prompt": prompt,
    }


def build_subagent_stop_payload(
    *, session_id: str, cwd: str, agent_name: str, response: str = ""
) -> dict[str, Any]:
    return {
        **_event_base("SubagentStop", session_id, cwd),
        "agent_name": agent_name,
        "response": response,
    }


def build_pre_compact_payload(
    *, session_id: str, cwd: str, trigger: str, token_count: int
) -> dict[str, Any]:
    return {
        **_event_base("PreCompact", session_id, cwd),
        "trigger": trigger,
        "token_count": token_count,
    }


def build_post_compact_payload(
    *, session_id: str, cwd: str, trigger: str, estimated_token_count: int
) -> dict[str, Any]:
    return {
        **_event_base("PostCompact", session_id, cwd),
        "trigger": trigger,
        "estimated_token_count": estimated_token_count,
    }


def build_notification_payload(
    *,
    session_id: str,
    cwd: str,
    sink: str,
    notification_type: str,
    title: str = "",
    body: str = "",
    severity: str = "info",
) -> dict[str, Any]:
    return {
        **_event_base("Notification", session_id, cwd),
        "sink": sink,
        "notification_type": notification_type,
        "title": title,
        "body": body,
        "severity": severity,
    }


# ---------------------------------------------------------------------------
# Hook fire helpers (sync; async variants delegate to run_hook_point_async)
# ---------------------------------------------------------------------------

def run_user_prompt_submit(
    prompt: str,
    session_id: str,
    project_root: str,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute UserPromptSubmit hook point."""
    return run_hook_point(
        HookPoint.USER_PROMPT_SUBMIT,
        payload=build_user_prompt_submit_payload(
            session_id=session_id, cwd=project_root, prompt=prompt
        ),
        project_root=project_root,
        settings=settings,
    )


def run_stop(
    session_id: str,
    project_root: str,
    stop_hook_active: bool = False,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute Stop hook point (agent finished; hooks may set continue=false)."""
    return run_hook_point(
        HookPoint.STOP,
        payload=build_stop_payload(
            session_id=session_id, cwd=project_root, stop_hook_active=stop_hook_active
        ),
        project_root=project_root,
        settings=settings,
    )


def run_stop_failure(
    session_id: str,
    project_root: str,
    error_type: str,
    error_message: str,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute StopFailure hook point."""
    return run_hook_point(
        HookPoint.STOP_FAILURE,
        payload=build_stop_failure_payload(
            session_id=session_id,
            cwd=project_root,
            error_type=error_type,
            error_message=error_message,
        ),
        project_root=project_root,
        settings=settings,
    )


def run_post_tool_use_failure(
    tool_name: str,
    args: dict[str, Any],
    error: str | Exception,
    context: ToolExecutionContext,
    settings: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
) -> MergedHookOutcome:
    """Execute PostToolUseFailure hook point (distinct from legacy ToolError)."""
    return run_hook_point(
        HookPoint.POST_TOOL_USE_FAILURE,
        payload=build_post_tool_use_failure_payload(
            session_id=str(getattr(context, "session_id", "default")),
            cwd=getattr(context, "project_root", "."),
            tool_name=tool_name,
            tool_input=args,
            error=str(error),
        ),
        project_root=getattr(context, "project_root", "."),
        settings=settings,
        timeout_s=timeout_s,
    )


def run_session_start(
    session_id: str,
    project_root: str,
    source: str = "startup",
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute SessionStart hook point."""
    return run_hook_point(
        HookPoint.SESSION_START,
        payload=build_session_start_payload(
            session_id=session_id, cwd=project_root, source=source
        ),
        project_root=project_root,
        settings=settings,
    )


def run_session_end(
    session_id: str,
    project_root: str,
    reason: str = "exit",
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute SessionEnd hook point."""
    return run_hook_point(
        HookPoint.SESSION_END,
        payload=build_session_end_payload(
            session_id=session_id, cwd=project_root, reason=reason
        ),
        project_root=project_root,
        settings=settings,
    )


def run_subagent_start(
    session_id: str,
    project_root: str,
    agent_name: str,
    prompt: str,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute SubagentStart hook point."""
    return run_hook_point(
        HookPoint.SUBAGENT_START,
        payload=build_subagent_start_payload(
            session_id=session_id, cwd=project_root, agent_name=agent_name, prompt=prompt
        ),
        project_root=project_root,
        settings=settings,
    )


def run_subagent_stop(
    session_id: str,
    project_root: str,
    agent_name: str,
    response: str = "",
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute SubagentStop hook point."""
    return run_hook_point(
        HookPoint.SUBAGENT_STOP,
        payload=build_subagent_stop_payload(
            session_id=session_id, cwd=project_root, agent_name=agent_name, response=response
        ),
        project_root=project_root,
        settings=settings,
    )


def run_pre_compact(
    session_id: str,
    project_root: str,
    trigger: str,
    token_count: int,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute PreCompact hook point."""
    return run_hook_point(
        HookPoint.PRE_COMPACT,
        payload=build_pre_compact_payload(
            session_id=session_id,
            cwd=project_root,
            trigger=trigger,
            token_count=token_count,
        ),
        project_root=project_root,
        settings=settings,
    )


def run_post_compact(
    session_id: str,
    project_root: str,
    trigger: str,
    estimated_token_count: int,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute PostCompact hook point."""
    return run_hook_point(
        HookPoint.POST_COMPACT,
        payload=build_post_compact_payload(
            session_id=session_id,
            cwd=project_root,
            trigger=trigger,
            estimated_token_count=estimated_token_count,
        ),
        project_root=project_root,
        settings=settings,
    )


def run_notification(
    session_id: str,
    project_root: str,
    sink: str,
    notification_type: str,
    title: str = "",
    body: str = "",
    severity: str = "info",
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Execute Notification hook point."""
    return run_hook_point(
        HookPoint.NOTIFICATION,
        payload=build_notification_payload(
            session_id=session_id,
            cwd=project_root,
            sink=sink,
            notification_type=notification_type,
            title=title,
            body=body,
            severity=severity,
        ),
        project_root=project_root,
        settings=settings,
    )


async def run_user_prompt_submit_async(
    prompt: str,
    session_id: str,
    project_root: str,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Async UserPromptSubmit."""
    return await run_hook_point_async(
        HookPoint.USER_PROMPT_SUBMIT,
        payload=build_user_prompt_submit_payload(
            session_id=session_id, cwd=project_root, prompt=prompt
        ),
        project_root=project_root,
        settings=settings,
    )


async def run_stop_async(
    session_id: str,
    project_root: str,
    stop_hook_active: bool = False,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Async Stop."""
    return await run_hook_point_async(
        HookPoint.STOP,
        payload=build_stop_payload(
            session_id=session_id, cwd=project_root, stop_hook_active=stop_hook_active
        ),
        project_root=project_root,
        settings=settings,
    )


async def run_pre_compact_async(
    session_id: str,
    project_root: str,
    trigger: str,
    token_count: int,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Async PreCompact."""
    return await run_hook_point_async(
        HookPoint.PRE_COMPACT,
        payload=build_pre_compact_payload(
            session_id=session_id,
            cwd=project_root,
            trigger=trigger,
            token_count=token_count,
        ),
        project_root=project_root,
        settings=settings,
    )


async def run_post_compact_async(
    session_id: str,
    project_root: str,
    trigger: str,
    estimated_token_count: int,
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Async PostCompact."""
    return await run_hook_point_async(
        HookPoint.POST_COMPACT,
        payload=build_post_compact_payload(
            session_id=session_id,
            cwd=project_root,
            trigger=trigger,
            estimated_token_count=estimated_token_count,
        ),
        project_root=project_root,
        settings=settings,
    )


async def run_session_start_async(
    session_id: str,
    project_root: str,
    source: str = "startup",
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Async SessionStart."""
    return await run_hook_point_async(
        HookPoint.SESSION_START,
        payload=build_session_start_payload(
            session_id=session_id, cwd=project_root, source=source
        ),
        project_root=project_root,
        settings=settings,
    )


async def run_session_end_async(
    session_id: str,
    project_root: str,
    reason: str = "exit",
    settings: dict[str, Any] | None = None,
) -> MergedHookOutcome:
    """Async SessionEnd."""
    return await run_hook_point_async(
        HookPoint.SESSION_END,
        payload=build_session_end_payload(
            session_id=session_id, cwd=project_root, reason=reason
        ),
        project_root=project_root,
        settings=settings,
    )


@dataclass
class HookResult:
    """Result of a single hook execution."""

    action: str = "allow"  # "allow" | "block"
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False


async def run_hook(
    command: str,
    input_data: dict[str, Any],
    *,
    timeout: int = 30,
    cwd: str | None = None,
) -> HookResult:
    """Execute a single hook command. Fail-open: errors/timeouts -> allow."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=json.dumps(input_data).encode()),
                timeout=timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            await proc.wait()
            logger.warning("Hook timed out after %ss: %s", timeout, command)
            return HookResult(action="allow", timed_out=True)
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
    except Exception as e:
        logger.warning("Hook failed: %s: %s", command, e)
        return HookResult(action="allow", stderr=str(e))

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    exit_code = proc.returncode or 0

    if exit_code == 2:
        return HookResult(
            action="block",
            reason=stderr.strip(),
            stdout=stdout,
            stderr=stderr,
            exit_code=2,
        )

    if exit_code == 0 and stdout.strip():
        try:
            raw = json.loads(stdout)
            if isinstance(raw, dict):
                hook_output = raw.get("hookSpecificOutput", {})
                if isinstance(hook_output, dict) and hook_output.get("permissionDecision") == "deny":
                    return HookResult(
                        action="block",
                        reason=str(hook_output.get("permissionDecisionReason", "")),
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=0,
                    )
        except (json.JSONDecodeError, TypeError):
            pass

    return HookResult(action="allow", stdout=stdout, stderr=stderr, exit_code=exit_code)
