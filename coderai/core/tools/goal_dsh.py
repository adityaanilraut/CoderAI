"""Model-facing get_goal / create_goal / update_goal tools (harness parity).

Authority rules mirror ``packages/goal/tool-goal``: create/edit/pause/resume
reject subagent callers (a continuable child session id starts with ``sub_``);
``blocked`` requires a concrete ``blocked_reason`` and, during an automatic
goal round, at least ``blockedAfterConsecutiveRounds`` (default 3) admitted
rounds. ``update_goal`` copies the exact ``goal_id``+``revision`` from
``get_goal`` (compare-and-set).
"""

from __future__ import annotations

import json
from typing import Any

from coderai.core.goals_dsh import (
    GoalBlockReason,
    GoalError,
    GoalRef,
    get_dsh_goal_store,
    resolve_goal_defaults,
)
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str

UPDATE_ACTIONS = ("edit", "pause", "resume", "complete", "blocked")


def _is_subagent_caller(context: ToolExecutionContext | None) -> bool:
    if context is None:
        return False
    sid = str(getattr(context, "session_id", "") or "")
    return sid.startswith("sub_") or sid.startswith("agent_")


def _goal_value(goal: Any) -> dict[str, Any]:
    if goal is None:
        return {"goal": None}
    return goal.tool_value()


def _render_value(value: dict[str, Any]) -> str:
    return json.dumps(value)


def _validated_ref(args: dict[str, Any]) -> tuple[str, int]:
    goal_id = as_str(args.get("goal_id", "")).strip()
    raw_revision = args.get("revision")
    if not goal_id or goal_id != goal_id.strip():
        raise GoalError("goal_id must be non-empty", "GOAL_TOOL_INVALID_UPDATE")
    if raw_revision is None:
        raise GoalError("revision must be a positive safe integer", "GOAL_TOOL_INVALID_UPDATE")
    try:
        revision = int(raw_revision)
    except (TypeError, ValueError):
        raise GoalError("revision must be a positive safe integer", "GOAL_TOOL_INVALID_UPDATE")
    if revision < 1:
        raise GoalError("revision must be a positive safe integer", "GOAL_TOOL_INVALID_UPDATE")
    return goal_id, revision


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _has_round_cap(value: Any) -> bool:
    return isinstance(value, (int, float)) and value != 0


def _handle_goal_error(name: str, exc: GoalError) -> ToolResult:
    return ToolResult(ok=False, name=name, error=f"{exc.code}: {exc}")


async def handle_get_goal_tool(
    args: dict[str, Any], context: ToolExecutionContext | None
) -> ToolResult:
    del args
    store = get_dsh_goal_store(_project_root(context))
    session_id = _session_id(context)
    goal = store.get(session_id)
    value = _goal_value(goal)
    return ToolResult(
        ok=True,
        name="get_goal",
        output=_render_value(value),
        metadata=value,
    )


async def handle_create_goal_tool(
    args: dict[str, Any], context: ToolExecutionContext | None
) -> ToolResult:
    objective = as_str(args.get("objective", "")).strip()
    if not objective:
        return ToolResult(
            ok=False, name="create_goal", error="Missing required 'objective' argument."
        )
    if _is_subagent_caller(context):
        return ToolResult(
            ok=False,
            name="create_goal",
            error="GOAL_TOOL_AUTHORITY: goal creation rejects subagent authority.",
        )
    store = get_dsh_goal_store(_project_root(context))
    session_id = _session_id(context)
    try:
        max_rounds_raw = args.get("max_goal_rounds") or args.get("maxGoalRounds")
        max_rounds = int(max_rounds_raw) if max_rounds_raw is not None else None
    except (TypeError, ValueError):
        return ToolResult(
            ok=False,
            name="create_goal",
            error="max_goal_rounds must be a positive safe integer",
        )
    try:
        goal = store.create(session_id, objective, max_goal_rounds=max_rounds)
    except GoalError as exc:
        return _handle_goal_error("create_goal", exc)
    value = _goal_value(goal)
    return ToolResult(ok=True, name="create_goal", output=_render_value(value), metadata=value)


async def handle_update_goal_tool(
    args: dict[str, Any], context: ToolExecutionContext | None
) -> ToolResult:
    action = as_str(args.get("action", "")).strip().lower()
    if action not in UPDATE_ACTIONS:
        return ToolResult(
            ok=False,
            name="update_goal",
            error=f"GOAL_TOOL_INVALID_UPDATE: unknown action '{action}' "
            f"(expected edit | pause | resume | complete | blocked)",
        )
    try:
        goal_id, revision = _validated_ref(args)
    except GoalError as exc:
        return _handle_goal_error("update_goal", exc)

    objective = as_str(args.get("objective", "")).strip() or None
    raw_cap = args.get("max_goal_rounds")
    max_rounds: int | None = None
    if raw_cap is not None:
        try:
            max_rounds = int(raw_cap)
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                name="update_goal",
                error="GOAL_TOOL_INVALID_UPDATE: max_goal_rounds must be a positive safe integer",
            )
    blocked_reason = as_str(args.get("blocked_reason", "")).strip()

    store = get_dsh_goal_store(_project_root(context))
    session_id = _session_id(context)

    # Field-per-action validation (harness GOAL_TOOL_INVALID_UPDATE rules).
    if action == "edit":
        if _has_text(blocked_reason):
            return ToolResult(
                ok=False,
                name="update_goal",
                error="GOAL_TOOL_INVALID_UPDATE: blocked_reason is valid only with action blocked",
            )
        if _is_subagent_caller(context):
            return ToolResult(
                ok=False,
                name="update_goal",
                error="GOAL_TOOL_AUTHORITY: edit requires a direct top-level human request.",
            )
    elif action in ("pause", "resume"):
        if _has_text(objective) or _has_round_cap(raw_cap) or _has_text(blocked_reason):
            return ToolResult(
                ok=False,
                name="update_goal",
                error="GOAL_TOOL_INVALID_UPDATE: objective and max_goal_rounds are valid only "
                "with action edit; blocked_reason is valid only with action blocked",
            )
        if _is_subagent_caller(context):
            return ToolResult(
                ok=False,
                name="update_goal",
                error=f"GOAL_TOOL_AUTHORITY: {action} requires a direct top-level human request.",
            )
    else:  # complete | blocked
        if _has_text(objective) or _has_round_cap(raw_cap):
            return ToolResult(
                ok=False,
                name="update_goal",
                error="GOAL_TOOL_INVALID_UPDATE: objective and max_goal_rounds are valid only "
                "with action edit",
            )
        if action == "complete" and _has_text(blocked_reason):
            return ToolResult(
                ok=False,
                name="update_goal",
                error="GOAL_TOOL_INVALID_UPDATE: blocked_reason is valid only with action blocked",
            )
        if action == "blocked" and not _has_text(blocked_reason):
            return ToolResult(
                ok=False,
                name="update_goal",
                error="GOAL_TOOL_INVALID_UPDATE: blocked_reason is required with action blocked",
            )
        if action == "blocked" and store.in_goal_round(session_id):
            goal = store.get(session_id)
            defaults = resolve_goal_defaults()
            threshold = defaults["blocked_after_rounds"]
            if goal is not None and goal.rounds_started < threshold:
                return ToolResult(
                    ok=False,
                    name="update_goal",
                    error=f"GOAL_TOOL_BLOCK_THRESHOLD: blocked requires at least {threshold} "
                    f"consecutive goal rounds; current round is {goal.rounds_started}",
                )

    ref = GoalRef(id=goal_id, revision=revision)
    try:
        if action == "edit":
            goal = store.edit(session_id, ref, objective, max_rounds)
        elif action == "pause":
            goal = store.pause(session_id, ref)
        elif action == "resume":
            goal = store.resume(session_id, ref)
        elif action == "complete":
            goal = store.complete(session_id, ref)
        else:
            goal = store.block(
                session_id, ref, GoalBlockReason(code="model-reported", message=blocked_reason)
            )
    except GoalError as exc:
        return _handle_goal_error("update_goal", exc)

    value = _goal_value(goal)
    return ToolResult(ok=True, name="update_goal", output=_render_value(value), metadata=value)


def _project_root(context: ToolExecutionContext | None) -> str:
    return str(getattr(context, "project_root", ".") or ".") if context else "."


def _session_id(context: ToolExecutionContext | None) -> str:
    return str(getattr(context, "session_id", "default") or "default") if context else "default"
