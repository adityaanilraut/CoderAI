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


logger = logging.getLogger(__name__)

DEFAULT_HOOK_TIMEOUT_SECONDS = 10.0


class HookPoint(str, enum.Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    ON_TOOL_ERROR = "ToolError"
    PRE_TURN = "PreTurn"
    POST_TURN = "PostTurn"
    PRE_STEP = "PreStep"
    POST_STEP = "PostStep"
    PRE_PROMPT = "PrePrompt"
    POST_PROMPT = "PostPrompt"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    STOP_CRITERIA = "StopCriteria"
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
    # Step hooks
    "pre_step": "PreStep",
    "prestep": "PreStep",
    "post_step": "PostStep",
    "poststep": "PostStep",
    # Prompt hooks
    "pre_prompt": "PrePrompt",
    "preprompt": "PrePrompt",
    "post_prompt": "PostPrompt",
    "postprompt": "PostPrompt",
    # Stop / Session / Subagent hooks
    "stop_criteria": "StopCriteria",
    "stopcriteria": "StopCriteria",
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
