"""Session-bound slash goal actions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coderai.ui.shell.dispatch import ShellContext, SlashAction


async def execute_goal_command(ctx: ShellContext, args: str) -> SlashAction:
    """Save goals and explicitly run bounded attempts within the current session."""
    from coderai.ui.shell.dispatch import SlashAction, _emit
    from coderai.goals.core import Goal, get_goal_store
    from coderai.soul.session.goals import publish_goal_state

    tokens = args.split(None, 1)
    action = tokens[0].lower() if tokens else "list"
    rest = tokens[1].strip() if len(tokens) > 1 else ""
    if (
        action not in ("list", "add", "start", "pause", "done", "cancel")
        or (action != "list" and not rest)
        or (action == "list" and rest)
    ):
        _emit(
            ctx, "Usage: /goal [list|add <objective>|start <id>|pause <id>|done <id>|cancel <id>]"
        )
        return SlashAction.HANDLED
    try:
        if ctx.session_id is None:
            if action != "add":
                _emit(ctx, "No active session. Use /goal add <objective> first.")
                return SlashAction.HANDLED
            ctx.session_id = await ctx.mgr.create_empty_session(plan_mode=ctx.active_plan_mode)
        if not ctx.session_id:
            raise ValueError("A session could not be created for the goal.")
        sid = ctx.session_id
        store = get_goal_store(ctx.mgr.project_root)
        goal: Goal | None
        if action == "list":
            _emit(ctx, store.format_summary(sid))
        elif action == "add":
            goal = store.create(sid, rest, status="pending")
            publish_goal_state(ctx.mgr, sid, f"Saved pending goal [{goal.id}]: {goal.objective}")
            _emit(ctx, f"Added goal [{goal.id}]. Use /goal start {goal.id} to run it.")
        elif action == "start":
            if ctx.active_plan_mode:
                raise ValueError("Exit plan mode before starting goal execution.")
            await ctx.mgr.goal_runner.start(sid, rest)
        else:
            status = {"done": "completed", "cancel": "cancelled", "pause": "paused"}[action]
            goal = store.update(sid, rest, status=status)
            if goal is None:
                raise ValueError(f"Unknown goal '{rest}'. Use /goal list for exact IDs.")
            owned = ctx.mgr.goal_runner.stop_goal(sid, goal.id)
            publish_goal_state(ctx.mgr, sid, f"Goal [{goal.id}] {goal.status.upper()}.")
            if owned:
                ctx.mgr.interrupt_session(sid)
                task = ctx.mgr._turn_tasks.get(sid)
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
            _emit(ctx, f"Goal [{goal.id}] {goal.status.upper()}.")
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        _emit(ctx, f"Goal error: {exc}")
    return SlashAction.HANDLED
