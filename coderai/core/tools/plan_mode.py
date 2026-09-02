"""exit_plan_mode — leave Plan Mode while mutation tools stay in the schema (KV-cache stability)."""

from __future__ import annotations

import re

from typing import Any

from coderai.core.tools.types import ToolResult, as_str


_HEADING_RE = re.compile(r"^#\s+\S", re.MULTILINE)


def handle_exit_plan_mode_tool(args: dict[str, Any], context: Any) -> ToolResult:
    raw = args.get("plan") if args.get("plan") is not None else args.get("summary")
    plan = as_str(raw).strip()

    # DSH: must be called from an agent/session
    session_id = None
    if isinstance(context, dict):
        session_id = context.get("session_id")
    else:
        session_id = getattr(context, "session_id", None)

    # DSH: only available when plan mode is active
    is_plan_mode = False
    if isinstance(context, dict):
        is_plan_mode = bool(context.get("plan_mode") or context.get("planMode"))
    else:
        is_plan_mode = bool(
            getattr(context, "plan_mode", None) or getattr(context, "planMode", None)
        )
        # also check session manager entry if available
        if not is_plan_mode:
            mgr = getattr(context, "manager", None) or getattr(context, "session_manager", None)
            if mgr is not None and session_id:
                try:
                    entry = mgr._get_entry(session_id) if hasattr(mgr, "_get_entry") else {}
                    is_plan_mode = bool((entry or {}).get("planMode"))
                except Exception:
                    pass

    has_plan_key = "plan" in args and args.get("plan") is not None
    if not session_id:
        # No session context (e.g. test harness) — lenient: validate only non-empty for summary alias
        if has_plan_key and (not plan or not _HEADING_RE.search(plan)):
            return ToolResult(
                ok=False,
                name="exit_plan_mode",
                error="exit_plan_mode requires a non-empty markdown plan starting with a # heading",
            )
        if not plan:
            return ToolResult(
                ok=False,
                name="exit_plan_mode",
                error="exit_plan_mode requires a non-empty markdown plan starting with a # heading",
            )
        summary = plan
        return ToolResult(
            ok=True,
            name="exit_plan_mode",
            output=summary,
            metadata={"exitPlanMode": True, "summary": summary, "approved": True},
        )
    if not is_plan_mode:
        return ToolResult(
            ok=False,
            name="exit_plan_mode",
            error="exit_plan_mode is only available in plan mode",
        )
    # DSH: header validation applies when `plan` param is used; `summary` alias remains lenient
    if has_plan_key:
        if not plan or not _HEADING_RE.search(plan):
            return ToolResult(
                ok=False,
                name="exit_plan_mode",
                error="exit_plan_mode requires a non-empty markdown plan starting with a # heading",
            )
    elif not plan:
        return ToolResult(
            ok=False,
            name="exit_plan_mode",
            error="exit_plan_mode requires a non-empty markdown plan starting with a # heading",
        )

    summary = plan
    return ToolResult(
        ok=True,
        name="exit_plan_mode",
        output=summary,
        metadata={"exitPlanMode": True, "summary": summary, "approved": True},
    )
