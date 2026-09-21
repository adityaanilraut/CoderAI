"""Lifecycle Hooks Framework

Provides full event-driven lifecycle interception:
- Points: PreToolUse, PostToolUse, PreStep, PostStep, PrePrompt, PostPrompt, StopCriteria, SessionStart, SessionEnd.
- Pattern Matcher: exact, glob, regex, comma-delimited lists.
- Merge Precedence: 'deny' > 'ask' > 'allow' > 'none'.
- Actionability: Context injection (additionalContext), system messages, and halt triggers (continue: false).
- Environment & Payload: JSON stdin payload with subprocess execution and timeout controls.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)

DEFAULT_HOOK_TIMEOUT_SECONDS = 10.0

from typing import Literal
from pydantic import BaseModel, Field

HookEventType = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "StopFailure",
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "Notification",
]

HOOK_EVENT_TYPES: list[str] = [
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "StopFailure",
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "Notification",
]


class HookDef(BaseModel):
    """A single hook definition in config.toml."""

    event: HookEventType
    command: str
    matcher: str = ""
    timeout: int = Field(default=30, ge=1, le=600)


@dataclass
class HookOutput:
    """Decoded outcome from a single hook process execution."""

    decision: str = "none"  # "allow" | "ask" | "deny" | "none"
    reason: str | None = None
    continue_run: bool = True
    stop_reason: str | None = None
    additional_context: list[str] = field(default_factory=list)
    system_messages: list[str] = field(default_factory=list)
    updated_input: dict[str, Any] | None = None
    exit_code: int = 0
    duration_ms: float = 0.0
    raw_stdout: str = ""
    raw_stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "continue": self.continue_run,
            "stopReason": self.stop_reason,
            "additionalContext": self.additional_context,
            "systemMessages": self.system_messages,
            "updatedInput": self.updated_input,
            "exitCode": self.exit_code,
            "durationMs": self.duration_ms,
        }


@dataclass
class MergedHookOutcome:
    """The folded result across all hooks matching a given lifecycle point."""

    decision: str = "none"  # "deny" > "ask" > "allow" > "none"
    reason: str | None = None
    stop: bool = False
    stop_reason: str | None = None
    additional_context: list[str] = field(default_factory=list)
    system_messages: list[str] = field(default_factory=list)
    updated_input: dict[str, Any] | None = None

    def is_allowed(self) -> bool:
        return self.decision != "deny"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "stop": self.stop,
            "stopReason": self.stop_reason,
            "additionalContext": self.additional_context,
            "systemMessages": self.system_messages,
            "updatedInput": self.updated_input,
        }


def load_hook_config(project_root: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load hooks configuration from resolved settings or standard config files."""
    if isinstance(settings, dict) and isinstance(settings.get("hooks"), dict):
        return settings["hooks"]

    candidates = [
        pathlib.Path(project_root) / ".coderai" / "hooks.json",
        pathlib.Path(project_root) / ".claude" / "settings.json",
        pathlib.Path.home() / ".coderai" / "hooks.json",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("hooks"), dict):
                    return data["hooks"]
                # Direct top-level mapping
                if any(
                    k in data
                    for k in (
                        "PreToolUse",
                        "preToolUse",
                        "PostToolUse",
                        "postToolUse",
                        "PreStep",
                        "StopCriteria",
                    )
                ):
                    return data
        except Exception:
            continue
    return {}


def matches_hook_pattern(pattern: str, target: str) -> bool:
    """Check if target matches a hook matcher pattern (wildcard, comma list, or regex)."""
    p = (pattern or "*").strip()
    if p in ("", "*", "any"):
        return True

    # Comma or whitespace separated list
    parts = [part.strip() for part in p.replace(",", " ").split() if part.strip()]
    for part in parts:
        if part == "*" or part.lower() == target.lower() or part == target:
            return True
        if fnmatch.fnmatch(target.lower(), part.lower()):
            return True
        try:
            if re.match(part, target, re.IGNORECASE):
                return True
        except re.error:
            pass
    return False


def _rank_decision(decision: str) -> int:
    """Rank decision for precedence: deny (3) > ask (2) > allow (1) > none (0)."""
    d = (decision or "none").strip().lower()
    if d in ("deny", "block", "reject"):
        return 3
    if d in ("ask", "prompt"):
        return 2
    if d in ("allow", "approve", "accept"):
        return 1
    return 0


def merge_hook_outputs(outputs: list[HookOutput]) -> MergedHookOutcome:
    """Fold multiple hook execution outcomes by documented precedence."""
    max_rank = 0
    reasons: list[str] = []
    stop = False
    stop_reason: str | None = None
    all_context: list[str] = []
    all_sys_messages: list[str] = []
    updated_input: dict[str, Any] | None = None

    for out in outputs:
        r = _rank_decision(out.decision)
        if r > max_rank:
            max_rank = r

        if out.reason and r >= 2:  # record reasons for deny or ask
            reasons.append(out.reason)

        if not out.continue_run:
            stop = True
            if not stop_reason and out.stop_reason:
                stop_reason = out.stop_reason

        for ctx in out.additional_context:
            if ctx and ctx not in all_context:
                all_context.append(ctx)

        for sys_msg in out.system_messages:
            if sys_msg and sys_msg not in all_sys_messages:
                all_sys_messages.append(sys_msg)

        if out.updated_input is not None:
            updated_input = out.updated_input

    decision_map = {3: "deny", 2: "ask", 1: "allow", 0: "none"}
    return MergedHookOutcome(
        decision=decision_map.get(max_rank, "none"),
        reason="\n\n".join(reasons) if reasons else None,
        stop=stop,
        stop_reason=stop_reason,
        additional_context=all_context,
        system_messages=all_sys_messages,
        updated_input=updated_input,
    )
