"""Lifecycle Hooks Framework

Provides full event-driven lifecycle interception:
- Points: PreToolUse, PostToolUse, PreStep, PostStep, PrePrompt, PostPrompt, StopCriteria, SessionStart, SessionEnd.
- Pattern Matcher: exact, glob, regex, comma-delimited lists.
- Merge Precedence: 'deny' > 'ask' > 'allow' > 'none'.
- Actionability: Context injection (additionalContext), system messages, and halt triggers (continue: false).
- Environment & Payload: JSON stdin payload with subprocess execution and timeout controls.
"""

from __future__ import annotations

import enum
import logging
from typing import Any


logger = logging.getLogger(__name__)


class HookPoint(str, enum.Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    ON_TOOL_ERROR = "ToolError"
    PRE_TURN = "PreTurn"
    POST_TURN = "PostTurn"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    ON_SUBAGENT_SPAWN = "SubagentSpawn"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    NOTIFICATION = "Notification"


HOOK_POINT_ALIASES: dict[str, str] = {
    # Tool call hooks
    "pre_tool_call": "PreToolUse",
    "pretoolcall": "PreToolUse",
    "pre_tool_use": "PreToolUse",
    "pretooluse": "PreToolUse",
    "post_tool_call": "PostToolUse",
    "posttoolcall": "PostToolUse",
    "post_tool_use": "PostToolUse",
    "posttooluse": "PostToolUse",
    "on_tool_error": "ToolError",
    "tool_error": "ToolError",
    "toolerror": "ToolError",
    "onerror": "ToolError",
    # Turn hooks
    "pre_turn": "PreTurn",
    "preturn": "PreTurn",
    "post_turn": "PostTurn",
    "postturn": "PostTurn",
    # Stop / Session / Subagent hooks
    "stop": "Stop",
    "stop_failure": "StopFailure",
    "stopfailure": "StopFailure",
    "post_tool_use_failure": "PostToolUseFailure",
    "posttoolusefailure": "PostToolUseFailure",
    "user_prompt_submit": "UserPromptSubmit",
    "userpromptsubmit": "UserPromptSubmit",
    "session_start": "SessionStart",
    "sessionstart": "SessionStart",
    "session_end": "SessionEnd",
    "sessionend": "SessionEnd",
    "on_subagent_spawn": "SubagentSpawn",
    "subagent_spawn": "SubagentSpawn",
    "subagentspawn": "SubagentSpawn",
    "subagent_start": "SubagentStart",
    "subagentstart": "SubagentStart",
    "subagent_stop": "SubagentStop",
    "subagentstop": "SubagentStop",
    "pre_compact": "PreCompact",
    "precompact": "PreCompact",
    "post_compact": "PostCompact",
    "postcompact": "PostCompact",
    "notification": "Notification",
}


def normalize_hook_point(point: HookPoint | str) -> str:
    """Resolve any canonical HookPoint enum, string, or alias into its canonical name."""
    raw = point.value if isinstance(point, HookPoint) else str(point)
    cleaned = raw.strip().lower()
    return HOOK_POINT_ALIASES.get(cleaned, raw)


def _base(event: str, session_id: str, cwd: str) -> dict[str, Any]:
    return {"hook_event_name": event, "session_id": session_id, "cwd": cwd}


def pre_tool_use(
    *,
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_call_id: str = "",
) -> dict[str, Any]:
    return {
        **_base("PreToolUse", session_id, cwd),
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_call_id": tool_call_id,
    }


def post_tool_use(
    *,
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: str = "",
    tool_call_id: str = "",
) -> dict[str, Any]:
    return {
        **_base("PostToolUse", session_id, cwd),
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_output": tool_output,
        "tool_call_id": tool_call_id,
    }


def post_tool_use_failure(
    *,
    session_id: str,
    cwd: str,
    tool_name: str,
    tool_input: dict[str, Any],
    error: str,
    tool_call_id: str = "",
) -> dict[str, Any]:
    return {
        **_base("PostToolUseFailure", session_id, cwd),
        "tool_name": tool_name,
        "tool_input": tool_input,
        "error": error,
        "tool_call_id": tool_call_id,
    }


def user_prompt_submit(
    *,
    session_id: str,
    cwd: str,
    prompt: str,
) -> dict[str, Any]:
    return {**_base("UserPromptSubmit", session_id, cwd), "prompt": prompt}


def stop(
    *,
    session_id: str,
    cwd: str,
    stop_hook_active: bool = False,
) -> dict[str, Any]:
    return {
        **_base("Stop", session_id, cwd),
        "stop_hook_active": stop_hook_active,
    }


def stop_failure(
    *,
    session_id: str,
    cwd: str,
    error_type: str,
    error_message: str,
) -> dict[str, Any]:
    return {
        **_base("StopFailure", session_id, cwd),
        "error_type": error_type,
        "error_message": error_message,
    }


def session_start(
    *,
    session_id: str,
    cwd: str,
    source: str,
) -> dict[str, Any]:
    return {**_base("SessionStart", session_id, cwd), "source": source}


def session_end(
    *,
    session_id: str,
    cwd: str,
    reason: str,
) -> dict[str, Any]:
    return {**_base("SessionEnd", session_id, cwd), "reason": reason}


def subagent_start(
    *,
    session_id: str,
    cwd: str,
    agent_name: str,
    prompt: str,
) -> dict[str, Any]:
    return {
        **_base("SubagentStart", session_id, cwd),
        "agent_name": agent_name,
        "prompt": prompt,
    }


def subagent_stop(
    *,
    session_id: str,
    cwd: str,
    agent_name: str,
    response: str = "",
) -> dict[str, Any]:
    return {
        **_base("SubagentStop", session_id, cwd),
        "agent_name": agent_name,
        "response": response,
    }


def pre_compact(
    *,
    session_id: str,
    cwd: str,
    trigger: str,
    token_count: int,
) -> dict[str, Any]:
    return {
        **_base("PreCompact", session_id, cwd),
        "trigger": trigger,
        "token_count": token_count,
    }


def post_compact(
    *,
    session_id: str,
    cwd: str,
    trigger: str,
    estimated_token_count: int,
) -> dict[str, Any]:
    return {
        **_base("PostCompact", session_id, cwd),
        "trigger": trigger,
        "estimated_token_count": estimated_token_count,
    }


def notification(
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
        **_base("Notification", session_id, cwd),
        "sink": sink,
        "notification_type": notification_type,
        "title": title,
        "body": body,
        "severity": severity,
    }
