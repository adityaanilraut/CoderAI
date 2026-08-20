"""Schedule tools — create, list, and delete durable timers and reminders."""

from __future__ import annotations

import json
from typing import Any

from coderai.core.schedule import get_schedule_manager
from coderai.core.tools.types import ToolResult, as_str


def handle_schedule_create_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Create a new session reminder or recurring timer."""
    prompt = as_str(args.get("prompt", "")).strip()
    after_seconds = args.get("after_seconds")
    at = args.get("at")
    every_seconds = args.get("every_seconds")

    if not prompt:
        return ToolResult(
            ok=False,
            name="schedule_create",
            error="Missing required parameter `prompt`.",
        )

    mgr = get_schedule_manager()
    try:
        rec = mgr.create(
            prompt=prompt,
            after_seconds=after_seconds,
            at=at,
            every_seconds=every_seconds,
        )
        out = rec.to_dict()
        return ToolResult(
            ok=True,
            name="schedule_create",
            output=json.dumps(out, indent=2),
            metadata=out,
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="schedule_create",
            error=f"Failed to create schedule: {exc}",
        )


def handle_schedule_list_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """List all active and overdue session reminders."""
    mgr = get_schedule_manager()
    records = mgr.list_schedules()
    out = [r.to_dict() for r in records]
    return ToolResult(
        ok=True,
        name="schedule_list",
        output=json.dumps(out, indent=2),
        metadata={"schedules": out},
    )


def handle_schedule_delete_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Delete a scheduled reminder by id."""
    schedule_id = as_str(args.get("schedule_id") or args.get("id", "")).strip()
    if not schedule_id:
        return ToolResult(
            ok=False,
            name="schedule_delete",
            error="Missing required parameter `schedule_id`.",
        )

    mgr = get_schedule_manager()
    deleted = mgr.delete(schedule_id)
    if not deleted:
        return ToolResult(
            ok=False,
            name="schedule_delete",
            error=f"Schedule with id `{schedule_id}` not found.",
        )

    out = {"id": schedule_id, "deleted": True}
    return ToolResult(
        ok=True,
        name="schedule_delete",
        output=json.dumps(out, indent=2),
        metadata=out,
    )
