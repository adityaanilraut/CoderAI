"""enter_plan_mode — enter Plan Mode (counterpart to ``exit_plan_mode``).

CoderAI function-style handler (mirrors ``handle_exit_plan_mode_tool``):
validates session + not-already-planning, then returns ``enterPlanMode``
metadata for the session loop to flip ``planMode`` on (same pattern as
``exitPlanMode``). Plan-mode entry is permission-exempt session bookkeeping,
so no approval round-trip is needed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from coderai.tools.legacy.types import ToolResult

# NOTE: coderai.tools.legacy.types is imported inside the handler (not at
# module top) — see tools/plan/__init__.py for the registry-init cycle.

DESCRIPTION = (Path(__file__).parent / "enter_description.md").read_text(encoding="utf-8")


def _session_id_from(context: Any) -> str | None:
    if isinstance(context, dict):
        return context.get("session_id")
    return getattr(context, "session_id", None)


def _plan_mode_from(context: Any, session_id: str | None) -> bool:
    if isinstance(context, dict):
        return bool(context.get("plan_mode") or context.get("planMode"))
    is_plan_mode = bool(
        getattr(context, "plan_mode", None) or getattr(context, "planMode", None)
    )
    if not is_plan_mode:
        mgr = getattr(context, "manager", None) or getattr(context, "session_manager", None)
        if mgr is not None and session_id:
            try:
                entry = mgr._get_entry(session_id) if hasattr(mgr, "_get_entry") else {}
                is_plan_mode = bool((entry or {}).get("planMode"))
            except Exception:
                pass
            if not is_plan_mode and hasattr(mgr, "get_session_state"):
                try:
                    state = mgr.get_session_state(session_id)
                    is_plan_mode = bool(getattr(state, "plan_mode", False))
                except Exception:
                    pass
    return is_plan_mode


def handle_enter_plan_mode_tool(args: dict[str, Any], context: Any) -> ToolResult:
    from coderai.tools.legacy.types import ToolResult

    _ = args
    session_id = _session_id_from(context)
    if not session_id:
        return ToolResult(
            ok=False,
            name="enter_plan_mode",
            error="enter_plan_mode requires an active session",
        )
    if _plan_mode_from(context, session_id):
        return ToolResult(
            ok=True,
            name="enter_plan_mode",
            output="Already in plan mode. Use exit_plan_mode when your plan is ready.",
            metadata={"enterPlanMode": True},
        )
    output = (
        "Plan mode activated. You MUST NOT edit code files — only read and plan.\n"
        "Workflow: identify key questions about the codebase → "
        "use subagent explore to investigate if needed → "
        "design approach → write the session plan file → "
        "call exit_plan_mode.\n"
        "Use AskUserQuestion only to clarify missing requirements or choose "
        "between approaches. Do NOT use AskUserQuestion to ask about plan approval."
    )
    return ToolResult(
        ok=True,
        name="enter_plan_mode",
        output=output,
        metadata={"enterPlanMode": True},
    )
