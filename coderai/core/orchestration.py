"""Shared orchestration parity layer for CoderAI.

Mirrors the DeepSeek Harness subagent/workflow/goal seam vocabulary onto
CoderAI's asyncio primitives:

- ``SubagentStopReason`` — the harness stop-reason union and the mapping from
  CoderAI's internal result statuses (``completed``/``failed``/``interrupted``/
  ``timeout``/``max_iterations``/``budget_exceeded``/``refusal``).
- ``OrchestrationEventBus`` — contained process-local publication of the
  ``subagent/start`` + ``subagent/end`` and ``workflow/*`` lifecycle pairs.
  Listener failures are logged and contained; they never change the run.
- ``resolve_child_depth`` — lineage-derived delegation depth (parent + 1),
  monotone via the registry handle when available.
- ``resolve_workflow_limits`` / env knobs — deployment ceilings with DeepSeek
  Harness defaults, overridable via ``CODERAI_*`` environment variables.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stop-reason vocabulary (mirrors packages/subagent/subagent/src/types.ts)
# ---------------------------------------------------------------------------

SUBTASK_COMPLETED = "completed"
SUBTASK_ABORTED = "aborted"
SUBTASK_ERROR = "error"
SUBTASK_MAX_TOKENS = "max-tokens"
SUBTASK_REFUSAL = "refusal"

SUBTASK_STOP_REASONS: frozenset[str] = frozenset(
    {SUBTASK_COMPLETED, SUBTASK_ABORTED, SUBTASK_ERROR, SUBTASK_MAX_TOKENS, SUBTASK_REFUSAL}
)

# CoderAI legacy status -> harness stop reason.
_STATUS_TO_STOP_REASON: dict[str, str] = {
    "completed": SUBTASK_COMPLETED,
    "failed": SUBTASK_ERROR,
    "interrupted": SUBTASK_ABORTED,
    "timeout": SUBTASK_ABORTED,
    "max_iterations": SUBTASK_ABORTED,
    "budget_exceeded": SUBTASK_MAX_TOKENS,
    "refusal": SUBTASK_REFUSAL,
}


def status_to_stop_reason(status: str) -> str:
    """Map a CoderAI subagent result status onto the harness stop-reason union.

    Unknown terminal reasons are treated as failures rather than reporting
    partial output as success (mirrors the reference's fall-through rule).
    """
    return _STATUS_TO_STOP_REASON.get(status, SUBTASK_ERROR)


def stop_reason_error(stop_reason: str) -> str:
    """Parent-facing failure headline for a non-completed stop reason."""
    return {
        SUBTASK_ABORTED: "subagent run was cancelled",
        SUBTASK_ERROR: "subagent run failed",
        SUBTASK_MAX_TOKENS: "subagent run hit its token limit before finishing",
        SUBTASK_REFUSAL: "subagent declined the task",
    }.get(stop_reason, f"subagent run ended abnormally ({stop_reason})")


def settlement_summary(child_id: str, stop_reason: str, outcome: str | None = None) -> str:
    """One line telling a parent a background child settled and why."""
    subject = f"Background subagent {child_id}"
    base = {
        SUBTASK_COMPLETED: f"{subject} finished and will do no further work unless you send it more.",
        SUBTASK_ABORTED: f"{subject} was stopped before it finished.",
        SUBTASK_MAX_TOKENS: f"{subject} ran out of room before it finished.",
        SUBTASK_REFUSAL: f"{subject} declined the task.",
        SUBTASK_ERROR: f"{subject} failed before it finished.",
    }.get(stop_reason, f"{subject} ended abnormally ({stop_reason}) before it finished.")
    if outcome and str(outcome).strip():
        return f"{base}\nOutcome:\n{str(outcome).strip()}"
    return base


# ---------------------------------------------------------------------------
# Lifecycle event bus (mirrors subagent/start + subagent/end publication)
# ---------------------------------------------------------------------------


def _render_thrown(value: Any) -> str:
    try:
        return str(value)
    except Exception:  # pragma: no cover - defensive
        return "<unrenderable thrown value>"


class OrchestrationEventBus:
    """Process-local, contained lifecycle publisher.

    Subscribing callables receive ``(event_name, payload)``. Every listener is
    independently contained: a throw or rejection is logged without starving
    peer listeners or changing the run.
    """

    def __init__(self) -> None:
        self._listeners: list[Any] = []
        self._lock = threading.Lock()

    def subscribe(self, listener: Any) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unsubscribe(self, listener: Any) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def emit(self, event_name: str, payload: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                returned = listener(event_name, payload)
                if returned is not None and hasattr(returned, "add_done_callback"):
                    returned.add_done_callback(
                        lambda fut: (
                            logger.warning(
                                "orchestration: %s listener rejected: %s",
                                event_name,
                                _render_thrown(fut.exception() if fut.exception() else ""),
                            )
                            if fut.exception()
                            else None
                        )
                    )
            except Exception as exc:  # pragma: no cover - containment guard
                logger.warning(
                    "orchestration: %s listener threw: %s", event_name, _render_thrown(exc)
                )

    def clear(self) -> None:
        with self._lock:
            self._listeners.clear()


_event_bus = OrchestrationEventBus()


def get_orchestration_event_bus() -> OrchestrationEventBus:
    """Return the process-local lifecycle event bus singleton."""
    return _event_bus


def publish_subagent_start(
    *,
    run_id: str,
    provider: str,
    child_id: str,
    local: bool,
    parent_session_id: str | None = None,
) -> None:
    _event_bus.emit(
        "subagent/start",
        {
            "runId": run_id,
            "provider": provider,
            "id": child_id,
            "local": local,
            "parentSessionId": parent_session_id,
        },
    )


def publish_subagent_end(
    *,
    run_id: str,
    provider: str,
    child_id: str,
    local: bool,
    stop_reason: str,
    last_assistant_message: list[Any] | None = None,
    parent_session_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "runId": run_id,
        "provider": provider,
        "id": child_id,
        "local": local,
        "stopReason": stop_reason,
        "parentSessionId": parent_session_id,
    }
    if last_assistant_message:
        payload["lastAssistantMessage"] = last_assistant_message
    _event_bus.emit("subagent/end", payload)


# ---------------------------------------------------------------------------
# Delegation depth (mirrors packages/subagent/subagent/src/depth.ts)
# ---------------------------------------------------------------------------

DEFAULT_MAX_SUBAGENT_DEPTH = 3


def resolve_child_depth(parent_depth: int | None, max_depth: int | None = None) -> int:
    """Compute a child's delegation depth: zero for top level, parent + 1 below.

    The cap itself is enforced by the quota check at spawn time (kept for
    backward compatibility with ``check_subagent_depth_quota``); this helper is
    the lineage-derived depth source so children can never reset to zero.
    """
    parent = parent_depth if parent_depth is not None and parent_depth >= 0 else 0
    child = parent + 1
    if max_depth is not None and child > max_depth:
        child = max_depth
    return child


# ---------------------------------------------------------------------------
# Deployment limits (mirrors workflow-worker-thread Config defaults)
# ---------------------------------------------------------------------------


class WorkflowLimits:
    """Resolved per-run workflow ceilings."""

    def __init__(
        self,
        max_concurrent_agents: int,
        max_total_agents: int,
        max_items_per_call: int,
        sync_timeout_ms: int = 5000,
        dispose_grace_ms: int = 5000,
    ) -> None:
        self.max_concurrent_agents = max_concurrent_agents
        self.max_total_agents = max_total_agents
        self.max_items_per_call = max_items_per_call
        self.sync_timeout_ms = sync_timeout_ms
        self.dispose_grace_ms = dispose_grace_ms


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def auto_max_concurrent_agents() -> int:
    """DSH default: min(16, max(1, cores - 2))."""
    cores = os.cpu_count() or 1
    return min(16, max(1, cores - 2))


def resolve_workflow_limits(settings: dict[str, Any] | None = None) -> WorkflowLimits:
    """Resolve workflow ceilings from settings/env with harness defaults."""
    settings = settings or {}
    orch = settings.get("orchestration") or {}

    def _pick(settings_key: str, env_name: str, default: int) -> int:
        from_settings = orch.get(settings_key)
        if isinstance(from_settings, int) and from_settings >= 1:
            return from_settings
        return _env_int(env_name, default)

    concurrent = _pick("maxConcurrentAgents", "CODERAI_WORKFLOW_MAX_CONCURRENT_AGENTS", 0)
    return WorkflowLimits(
        max_concurrent_agents=(auto_max_concurrent_agents() if concurrent <= 0 else concurrent),
        max_total_agents=_pick("maxTotalAgents", "CODERAI_WORKFLOW_MAX_TOTAL_AGENTS", 1000),
        max_items_per_call=_pick("maxItemsPerCall", "CODERAI_WORKFLOW_MAX_ITEMS_PER_CALL", 4096),
    )


def resolve_subagent_defaults(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Env/settings-driven subagent defaults (depth, timeout, max iterations)."""
    settings = settings or {}
    orch = settings.get("orchestration") or {}

    def _float_pick(settings_key: str, env_name: str, default: float) -> float:
        from_settings = orch.get(settings_key)
        if isinstance(from_settings, (int, float)) and from_settings > 0:
            return float(from_settings)
        raw = os.environ.get(env_name)
        try:
            value = float(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _int_pick(settings_key: str, env_name: str, default: int) -> int:
        from_settings = orch.get(settings_key)
        if isinstance(from_settings, int) and from_settings >= 1:
            return from_settings
        return _env_int(env_name, default)

    return {
        "max_depth": _int_pick(
            "maxDepth", "CODERAI_MAX_SUBAGENT_DEPTH", DEFAULT_MAX_SUBAGENT_DEPTH
        ),
        "timeout_seconds": _float_pick("timeoutSeconds", "CODERAI_SUBAGENT_TIMEOUT_SECONDS", 90.0),
        "max_iterations": _int_pick("maxIterations", "CODERAI_SUBAGENT_MAX_ITERATIONS", 20),
    }


def resolve_ralph_max_rounds(settings: dict[str, Any] | None = None) -> int:
    settings = settings or {}
    orch = settings.get("orchestration") or {}
    from_settings = orch.get("ralphMaxRounds")
    if isinstance(from_settings, int) and from_settings >= 1:
        return from_settings
    return _env_int("CODERAI_RALPH_MAX_ROUNDS", 256)


def resolve_goal_defaults(settings: dict[str, Any] | None = None) -> dict[str, int]:
    settings = settings or {}
    orch = settings.get("orchestration") or {}
    max_rounds = orch.get("goalMaxRounds")
    if not (isinstance(max_rounds, int) and max_rounds >= 1):
        max_rounds = _env_int("CODERAI_GOAL_MAX_ROUNDS", 256)
    blocked_after = orch.get("goalBlockedAfterRounds")
    if not (isinstance(blocked_after, int) and blocked_after >= 1):
        blocked_after = _env_int("CODERAI_GOAL_BLOCKED_AFTER_ROUNDS", 3)
    return {"max_goal_rounds": max_rounds, "blocked_after_rounds": blocked_after}


def resolve_max_parallel_tool_calls(settings: dict[str, Any] | None = None) -> int:
    """DSH agent-loop default: 10 parallel tool calls in one rolling pool."""
    settings = settings or {}
    orch = settings.get("orchestration") or {}
    from_settings = orch.get("maxParallelToolCalls")
    if isinstance(from_settings, int) and from_settings >= 1:
        return from_settings
    return _env_int("CODERAI_MAX_PARALLEL_TOOL_CALLS", 10)


DEFAULT_MAX_CONTINUABLE_AGENTS: int = 50
DEFAULT_MAX_RUNNING_JOBS: int = 50


def resolve_max_continuable_agents(settings: dict[str, Any] | None = None) -> int:
    """Resolve maximum live continuable subagents per session (env, settings, or default 50)."""
    settings = settings or {}
    orch = settings.get("orchestration") or {}
    from_settings = orch.get("maxContinuableAgents")
    if isinstance(from_settings, int) and from_settings >= 1:
        return from_settings
    val = _env_int("CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION", 0)
    if val >= 1:
        return val
    val = _env_int("MAX_CONTINUABLE_AGENTS_PER_SESSION", 0)
    if val >= 1:
        return val
    return DEFAULT_MAX_CONTINUABLE_AGENTS


def resolve_max_running_jobs(settings: dict[str, Any] | None = None) -> int:
    """Resolve maximum concurrent running background jobs per session (env, settings, or default 50)."""
    settings = settings or {}
    orch = settings.get("orchestration") or {}
    from_settings = orch.get("maxRunningJobs")
    if isinstance(from_settings, int) and from_settings >= 1:
        return from_settings
    val = _env_int("CODERAI_MAX_RUNNING_JOBS_PER_SESSION", 0)
    if val >= 1:
        return val
    val = _env_int("MAX_RUNNING_JOBS_PER_SESSION", 0)
    if val >= 1:
        return val
    return DEFAULT_MAX_RUNNING_JOBS
