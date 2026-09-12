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
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderai.tools.legacy.types import ToolExecutionContext

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
        if pat.search(k) or k.upper().startswith("CODERAI_"):
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
        from coderai.wire.emitter import get_emitter

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
            from coderai.wire.types import HookResolved

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


# ---------------------------------------------------------------------------
# Kimi-parity HookEngine class for agent lifecycle
# ---------------------------------------------------------------------------

from collections.abc import Awaitable, Callable
from coderai.hooks.config import HookDef, HookEventType
from coderai.hooks.runner import HookResult, run_hook

OnTriggered = Callable[[str, str, int], None]
OnResolved = Callable[[str, str, str, str, int], None]
OnWireHookRequest = Callable[["WireHookHandle"], Awaitable[None]]


@dataclass
class WireHookSubscription:
    """A client-side hook subscription registered via wire initialize."""

    id: str
    event: str
    matcher: str = ""
    timeout: int = 30


@dataclass
class WireHookHandle:
    """A pending wire hook request waiting for client response."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    subscription_id: str = ""
    event: str = ""
    target: str = ""
    input_data: dict[str, Any] = field(default_factory=lambda: {})
    _future: asyncio.Future[HookResult] | None = field(default=None, repr=False)

    def _get_future(self) -> asyncio.Future[HookResult]:
        if self._future is None:
            self._future = asyncio.get_event_loop().create_future()
        return self._future

    async def wait(self) -> HookResult:
        return await self._get_future()

    def resolve(self, action: str = "allow", reason: str = "") -> None:
        result = HookResult(action=action, reason=reason)
        future = self._get_future()
        if not future.done():
            future.set_result(result)


class HookEngine:
    """Loads hook definitions and executes matching hooks in parallel."""

    def __init__(
        self,
        hooks: list[HookDef] | None = None,
        cwd: str | None = None,
        *,
        on_triggered: OnTriggered | None = None,
        on_resolved: OnResolved | None = None,
        on_wire_hook: OnWireHookRequest | None = None,
    ):
        self._hooks: list[HookDef] = list(hooks) if hooks else []
        self._wire_subs: list[WireHookSubscription] = []
        self._cwd = cwd
        self._on_triggered = on_triggered
        self._on_resolved = on_resolved
        self._on_wire_hook = on_wire_hook
        self._by_event: dict[str, list[HookDef]] = {}
        self._wire_by_event: dict[str, list[WireHookSubscription]] = {}
        self._pending_fire_and_forget: set[asyncio.Task[Any]] = set()
        self._rebuild_index()

    def fire_and_forget_trigger(
        self,
        event: HookEventType,
        *,
        matcher_value: str = "",
        input_data: dict[str, Any],
    ) -> asyncio.Task[list[HookResult]]:
        task: asyncio.Task[list[HookResult]] = asyncio.create_task(
            self.trigger(event, matcher_value=matcher_value, input_data=input_data)
        )
        self._pending_fire_and_forget.add(task)
        task.add_done_callback(self._pending_fire_and_forget.discard)
        return task

    def _rebuild_index(self) -> None:
        self._by_event.clear()
        for h in self._hooks:
            self._by_event.setdefault(h.event, []).append(h)
        self._wire_by_event.clear()
        for s in self._wire_subs:
            self._wire_by_event.setdefault(s.event, []).append(s)

    def add_hooks(self, hooks: list[HookDef]) -> None:
        self._hooks.extend(hooks)
        self._rebuild_index()

    def add_wire_subscriptions(self, subs: list[WireHookSubscription]) -> None:
        self._wire_subs.extend(subs)
        self._rebuild_index()

    def set_callbacks(
        self,
        on_triggered: OnTriggered | None = None,
        on_resolved: OnResolved | None = None,
        on_wire_hook: OnWireHookRequest | None = None,
    ) -> None:
        self._on_triggered = on_triggered
        self._on_resolved = on_resolved
        self._on_wire_hook = on_wire_hook

    @property
    def has_hooks(self) -> bool:
        return bool(self._hooks) or bool(self._wire_subs)

    def has_hooks_for(self, event: HookEventType) -> bool:
        return bool(self._by_event.get(event)) or bool(self._wire_by_event.get(event))

    def _match_regex(self, pattern: str, value: str) -> bool:
        if not pattern:
            return True
        try:
            return bool(re.search(pattern, value))
        except re.error:
            return False

    async def trigger(
        self,
        event: HookEventType,
        *,
        matcher_value: str = "",
        input_data: dict[str, Any],
    ) -> list[HookResult]:
        seen_commands: set[str] = set()
        server_matched: list[HookDef] = []
        for h in self._by_event.get(event, []):
            if not self._match_regex(h.matcher, matcher_value):
                continue
            if h.command in seen_commands:
                continue
            seen_commands.add(h.command)
            server_matched.append(h)

        wire_matched: list[WireHookSubscription] = []
        for s in self._wire_by_event.get(event, []):
            if not self._match_regex(s.matcher, matcher_value):
                continue
            wire_matched.append(s)

        total = len(server_matched) + len(wire_matched)
        if total == 0:
            return []

        try:
            results = await self._execute_hooks(
                event, matcher_value, server_matched, wire_matched, input_data
            )
        except Exception:
            logger.warning("Hook engine error for %s, failing open", event)
            return []

        return results

    async def _execute_hooks(
        self,
        event: str,
        matcher_value: str,
        server_matched: list[HookDef],
        wire_matched: list[WireHookSubscription],
        input_data: dict[str, Any],
    ) -> list[HookResult]:
        total = len(server_matched) + len(wire_matched)
        if self._on_triggered:
            try:
                self._on_triggered(event, matcher_value, total)
            except Exception:
                pass

        t0 = time.monotonic()
        tasks: list[asyncio.Task[HookResult]] = []

        for h in server_matched:
            tasks.append(
                asyncio.create_task(
                    run_hook(h.command, input_data, timeout=h.timeout, cwd=self._cwd)
                )
            )

        for s in wire_matched:
            tasks.append(
                asyncio.create_task(
                    self._dispatch_wire_hook(
                        s.id, event, matcher_value, input_data, timeout=s.timeout
                    )
                )
            )

        results = list(await asyncio.gather(*tasks))
        duration_ms = int((time.monotonic() - t0) * 1000)

        action = "allow"
        reason = ""
        for r in results:
            if r.action == "block":
                action = "block"
                reason = r.reason
                break

        if self._on_resolved:
            try:
                self._on_resolved(event, matcher_value, action, reason, duration_ms)
            except Exception:
                pass

        return results

    async def _dispatch_wire_hook(
        self,
        subscription_id: str,
        event: str,
        target: str,
        input_data: dict[str, Any],
        *,
        timeout: int = 30,
    ) -> HookResult:
        if not self._on_wire_hook:
            return HookResult(action="allow")

        handle = WireHookHandle(
            subscription_id=subscription_id,
            event=event,
            target=target,
            input_data=input_data,
        )
        hook_task = asyncio.ensure_future(self._on_wire_hook(handle))
        try:
            return await asyncio.wait_for(handle.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            hook_task.cancel()
            return HookResult(action="allow", timed_out=True)
        except Exception:
            hook_task.cancel()
            return HookResult(action="allow")
