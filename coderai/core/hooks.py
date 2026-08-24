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

from coderai.core.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

DEFAULT_HOOK_TIMEOUT_SECONDS = 10.0


class HookPoint(str, enum.Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_STEP = "PreStep"
    POST_STEP = "PostStep"
    PRE_PROMPT = "PrePrompt"
    POST_PROMPT = "PostPrompt"
    STOP_CRITERIA = "StopCriteria"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"


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


def execute_hook_command(
    command: str,
    payload: dict[str, Any],
    project_root: str,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
    env_vars: dict[str, str] | None = None,
) -> HookOutput:
    """Execute a single hook shell command with a JSON payload on stdin."""
    run_env = os.environ.copy()
    point_name = str(payload.get("hook_event_name") or "PreToolUse")
    run_env["CODERAI_HOOK_EVENT"] = point_name
    run_env["CODERAI_PROJECT_DIR"] = project_root
    run_env["CLAUDE_PROJECT_DIR"] = project_root
    if env_vars:
        run_env.update(env_vars)

    start_time = time.time()
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False) + "\n",
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=project_root,
            shell=True,
            env=run_env,
        )
        elapsed_ms = (time.time() - start_time) * 1000.0
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode != 0:
            return HookOutput(
                decision="deny",
                reason=f"Hook command exited with non-zero status {proc.returncode}: {stderr or stdout}",
                exit_code=proc.returncode,
                duration_ms=elapsed_ms,
                raw_stdout=stdout,
                raw_stderr=stderr,
            )

        if not stdout:
            return HookOutput(
                decision="none",
                exit_code=0,
                duration_ms=elapsed_ms,
            )

        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                decision_raw = str(parsed.get("decision", "none")).lower()
                decision = (
                    "deny"
                    if decision_raw in ("deny", "block", "reject")
                    else "ask"
                    if decision_raw in ("ask", "prompt")
                    else "allow"
                    if decision_raw in ("allow", "approve", "accept")
                    else "none"
                )

                continue_run = bool(parsed.get("continue", True))
                if str(parsed.get("action", "")).lower() == "stop":
                    continue_run = False

                add_ctx = parsed.get("additionalContext") or parsed.get("additional_context") or []
                if isinstance(add_ctx, str):
                    add_ctx = [add_ctx]

                sys_msgs = parsed.get("systemMessages") or parsed.get("system_messages") or []
                if isinstance(sys_msgs, str):
                    sys_msgs = [sys_msgs]

                return HookOutput(
                    decision=decision,
                    reason=parsed.get("reason"),
                    continue_run=continue_run,
                    stop_reason=parsed.get("stopReason") or parsed.get("stop_reason"),
                    additional_context=list(add_ctx),
                    system_messages=list(sys_msgs),
                    updated_input=parsed.get("updatedInput") or parsed.get("updated_input"),
                    exit_code=0,
                    duration_ms=elapsed_ms,
                    raw_stdout=stdout,
                    raw_stderr=stderr,
                )
        except json.JSONDecodeError:
            # Check simple string response
            if stdout.lower() in ("block", "deny", "reject"):
                return HookOutput(
                    decision="deny", exit_code=0, duration_ms=elapsed_ms, raw_stdout=stdout
                )
            elif stdout.lower() in ("allow", "approve", "accept"):
                return HookOutput(
                    decision="allow", exit_code=0, duration_ms=elapsed_ms, raw_stdout=stdout
                )

        return HookOutput(decision="none", exit_code=0, duration_ms=elapsed_ms, raw_stdout=stdout)

    except subprocess.TimeoutExpired:
        elapsed_ms = (time.time() - start_time) * 1000.0
        return HookOutput(
            decision="deny",
            reason=f"Hook command timed out after {timeout_s}s",
            exit_code=-1,
            duration_ms=elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = (time.time() - start_time) * 1000.0
        return HookOutput(
            decision="deny",
            reason=f"Hook execution failed: {exc}",
            exit_code=-1,
            duration_ms=elapsed_ms,
        )


def run_hook_point(
    point: HookPoint | str,
    payload: dict[str, Any],
    project_root: str,
    settings: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
) -> MergedHookOutcome:
    """Run all configured hooks matching the lifecycle point and target name."""
    point_name = point.value if isinstance(point, HookPoint) else str(point)
    config = load_hook_config(project_root, settings)
    if not config:
        return MergedHookOutcome()

    # Look for matching point configurations
    entries: list[dict[str, Any]] = []
    for key in (point_name, point_name[0].lower() + point_name[1:]):
        val = config.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    entries.append(item)
        elif isinstance(val, dict):
            entries.append(val)

    if not entries:
        return MergedHookOutcome()

    target_name = str(payload.get("tool_name") or payload.get("step") or "*")
    payload["hook_event_name"] = point_name
    payload["cwd"] = project_root

    outputs: list[HookOutput] = []
    for entry in entries:
        matcher = str(entry.get("matcher") or "*")
        if not matches_hook_pattern(matcher, target_name):
            continue

        hooks = entry.get("hooks") or entry.get("commands") or []
        if isinstance(hooks, (str, dict)):
            hooks = [hooks]

        for hook in hooks:
            command = hook.get("command") if isinstance(hook, dict) else hook
            if not isinstance(command, str) or not command.strip():
                continue
            hook_timeout_raw: Any = hook.get("timeout") if isinstance(hook, dict) else None
            hook_timeout = float(hook_timeout_raw) if hook_timeout_raw else timeout_s
            out = execute_hook_command(
                command=command,
                payload=payload,
                project_root=project_root,
                timeout_s=hook_timeout,
            )
            outputs.append(out)

    return merge_hook_outputs(outputs)


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
