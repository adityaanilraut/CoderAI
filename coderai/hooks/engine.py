# Ported from coderai/core/hooks.py - kimi structure (hooks/engine.py).
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

from coderai.core.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

DEFAULT_HOOK_TIMEOUT_SECONDS = 10.0
from coderai.hooks.config import (
    HookOutput,
    MergedHookOutcome,
    load_hook_config,
    matches_hook_pattern,
    merge_hook_outputs,
)
from coderai.hooks.events import HOOK_POINT_ALIASES, HookPoint, normalize_hook_point
def _scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    import re as _re

    pat = _re.compile(r"KEY|PASSWORD|SECRET|TOKEN", _re.I)
    src = base if base is not None else os.environ
    out: dict[str, str] = {}
    for k, v in src.items():
        if pat.search(k) or k.upper().startswith("DSH_"):
            continue
        out[k] = v
    return out


def execute_hook_command(
    command: str,
    payload: dict[str, Any],
    project_root: str,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
    env_vars: dict[str, str] | None = None,
) -> HookOutput:
    """Execute a single hook shell command with a JSON payload on stdin."""
    run_env = _scrubbed_env()
    if env_vars:
        for k, v in env_vars.items():
            run_env[k] = v
    point_name = str(payload.get("hook_event_name") or "PreToolUse")
    run_env["CODERAI_HOOK_EVENT"] = point_name
    run_env["CODERAI_PROJECT_DIR"] = project_root
    run_env["CLAUDE_PROJECT_DIR"] = project_root
    from coderai.telemetry.sink import get_telemetry_collector

    collector = get_telemetry_collector()
    span = collector.start_span(
        name=f"hook:{point_name}",
        kind="hook",
        attributes={"command": command, "point": point_name},
    )

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
        return _decode_hook_output(stdout, stderr, proc.returncode, elapsed_ms, collector, span)
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


def _decode_hook_output(
    stdout: str,
    stderr: str,
    returncode: int,
    elapsed_ms: float,
    collector: Any,
    span: Any,
) -> HookOutput:
    """Decode raw stdout/stderr/returncode from hook execution with safe containment."""
    if returncode != 0:
        if collector and span:
            collector.end_span(
                span.span_id,
                status="error",
                error=f"Exit {returncode}",
                extra_attributes={"duration_ms": elapsed_ms},
            )
            collector.increment_counter("hook_errors", 1.0)
        return HookOutput(
            decision="deny",
            reason=f"Hook command exited with non-zero status {returncode}: {stderr or stdout}",
            exit_code=returncode,
            duration_ms=elapsed_ms,
            raw_stdout=stdout,
            raw_stderr=stderr,
        )

    if collector and span:
        collector.end_span(
            span.span_id,
            status="ok",
            extra_attributes={"duration_ms": elapsed_ms},
        )
        collector.increment_counter("hook_executions", 1.0)

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


async def execute_hook_command_async(
    command: str,
    payload: dict[str, Any],
    project_root: str,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
    env_vars: dict[str, str] | None = None,
) -> HookOutput:
    """Execute a single hook shell command asynchronously with JSON payload piped to stdin."""
    run_env = _scrubbed_env()
    point_name = str(payload.get("hook_event_name") or "PreToolUse")
    run_env["CODERAI_HOOK_EVENT"] = point_name
    run_env["CODERAI_PROJECT_DIR"] = project_root
    run_env["CLAUDE_PROJECT_DIR"] = project_root
    if env_vars:
        # allow explicit overrides even if sensitive, but don't re-inject scrubbed keys
        for k, v in env_vars.items():
            run_env[k] = v

    from coderai.telemetry.sink import get_telemetry_collector

    collector = get_telemetry_collector()
    span = collector.start_span(
        name=f"hook_async:{point_name}",
        kind="hook",
        attributes={"command": command, "point": point_name},
    )

    start_time = time.time()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_root,
            env=run_env,
        )
        input_data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=input_data),
                timeout=timeout_s,
            )
        except (asyncio.TimeoutError, TimeoutError):
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            elapsed_ms = (time.time() - start_time) * 1000.0
            return HookOutput(
                decision="deny",
                reason=f"Hook command timed out after {timeout_s}s",
                exit_code=-1,
                duration_ms=elapsed_ms,
            )

        elapsed_ms = (time.time() - start_time) * 1000.0
        stdout = (stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "").strip()
        stderr = (stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "").strip()
        return _decode_hook_output(
            stdout,
            stderr,
            proc.returncode if proc.returncode is not None else 0,
            elapsed_ms,
            collector,
            span,
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
    """Run all configured hooks matching the lifecycle point and target name synchronously."""
    point_name = normalize_hook_point(point)
    config = load_hook_config(project_root, settings)
    if not config:
        return MergedHookOutcome()

    possible_keys = {
        point_name,
        point_name[0].lower() + point_name[1:],
        point.value if isinstance(point, HookPoint) else str(point),
        str(point).lower(),
    }
    for alias_k, canonical_v in HOOK_POINT_ALIASES.items():
        if canonical_v == point_name:
            possible_keys.add(alias_k)

    entries: list[dict[str, Any]] = []
    for key in possible_keys:
        val = config.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    entries.append(item)
        elif isinstance(val, dict):
            entries.append(val)

    if not entries:
        return MergedHookOutcome()

    target_name = str(payload.get("tool_name") or payload.get("step") or payload.get("task") or "*")
    payload["hook_event_name"] = point_name
    payload["cwd"] = project_root

    # Kimi parity: HookTriggered/HookResolved wire events bracket execution.
    import time as _hook_t

    _wire = None
    try:
        from coderai.core.wire.emitter import get_emitter

        _wire = get_emitter()
    except Exception:
        _wire = None
    _hook_start = _hook_t.time()
    if _wire is not None:
        try:
            _wire.hook_triggered(point_name, target_name, max(1, len(entries)))
        except Exception:
            pass

    outputs: list[HookOutput] = []
    for entry in entries:
        matcher = str(entry.get("matcher") or "*")
        if not matches_hook_pattern(matcher, target_name):
            continue

        hooks = entry.get("hooks") or entry.get("commands") or (entry if "command" in entry else [])
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

    merged = merge_hook_outputs(outputs)
    if _wire is not None:
        try:
            from coderai.core.wire.types import HookResolved

            _wire.send(
                HookResolved(
                    event=point_name,
                    target=target_name,
                    action="block" if merged.decision == "deny" else "allow",
                    reason=merged.reason or "",
                    duration_ms=int((_hook_t.time() - _hook_start) * 1000),
                )
            )
        except Exception:
            pass
    return merged


async def run_hook_point_async(
    point: HookPoint | str,
    payload: dict[str, Any],
    project_root: str,
    settings: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
) -> MergedHookOutcome:
    """Run all configured hooks matching the lifecycle point and target name asynchronously."""
    point_name = normalize_hook_point(point)
    config = load_hook_config(project_root, settings)
    if not config:
        return MergedHookOutcome()

    possible_keys = {
        point_name,
        point_name[0].lower() + point_name[1:],
        point.value if isinstance(point, HookPoint) else str(point),
        str(point).lower(),
    }
    for alias_k, canonical_v in HOOK_POINT_ALIASES.items():
        if canonical_v == point_name:
            possible_keys.add(alias_k)

    entries: list[dict[str, Any]] = []
    for key in possible_keys:
        val = config.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    entries.append(item)
        elif isinstance(val, dict):
            entries.append(val)

    if not entries:
        return MergedHookOutcome()

    target_name = str(payload.get("tool_name") or payload.get("step") or payload.get("task") or "*")
    payload["hook_event_name"] = point_name
    payload["cwd"] = project_root

    outputs: list[HookOutput] = []
    for entry in entries:
        matcher = str(entry.get("matcher") or "*")
        if not matches_hook_pattern(matcher, target_name):
            continue

        hooks = entry.get("hooks") or entry.get("commands") or (entry if "command" in entry else [])
        if isinstance(hooks, (str, dict)):
            hooks = [hooks]

        for hook in hooks:
            command = hook.get("command") if isinstance(hook, dict) else hook
            if not isinstance(command, str) or not command.strip():
                continue
            hook_timeout_raw: Any = hook.get("timeout") if isinstance(hook, dict) else None
            hook_timeout = float(hook_timeout_raw) if hook_timeout_raw else timeout_s
            out = await execute_hook_command_async(
                command=command,
                payload=payload,
                project_root=project_root,
                timeout_s=hook_timeout,
            )
            outputs.append(out)

    return merge_hook_outputs(outputs)
