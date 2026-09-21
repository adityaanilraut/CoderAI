"""UpdatePlan tool — updates the task plan."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coderai.tools.legacy.types import ToolResult

# ponytail: coderai imports are function-level so `import coderai.tools.todo`
# first never suspends this module partial while the tool registry instantiates.


def _validate_update_plan_schema(args: dict[str, Any]) -> tuple[bool, dict[str, Any], str | None]:
    plan = args.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        return False, {}, "plan must be a non-empty string."

    explanation = args.get("explanation")
    if explanation is not None and not isinstance(explanation, str):
        return False, {}, "explanation must be a string."

    validated = {"plan": plan}
    if isinstance(explanation, str) and explanation.strip():
        validated["explanation"] = explanation.strip()

    return True, validated, None


def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return handle_update_plan_tool(args, context)


def handle_update_plan_tool(args: dict[str, Any], context: Any) -> ToolResult:
    from coderai.utils.common.validate import execute_validated_tool
    from coderai.tools.legacy.types import ToolResult

    def run(validated_args: dict[str, Any], _ctx: Any) -> ToolResult:
        metadata: dict[str, Any] = {"plan": validated_args["plan"]}
        if "explanation" in validated_args:
            metadata["explanation"] = validated_args["explanation"]

        return ToolResult(
            ok=True,
            name="UpdatePlan",
            output="Plan updated.",
            metadata=metadata,
        )

    return execute_validated_tool(
        "UpdatePlan",
        args,
        context,
        run,
        validator=_validate_update_plan_schema,
    )


# --- from coderai/core/tools/todo_write.py ---
"""todo_write wraps UpdatePlan with a structured todo list."""


VALID_TODO_STATUS = ("pending", "in_progress", "completed", "cancelled")


def todos_to_plan(todos: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in todos:
        content = str(item.get("content") or item.get("title") or "").strip()
        if not content:
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status in ("completed", "done"):
            mark = "x"
        elif status in ("in_progress", "active"):
            mark = ">"
        elif status in ("cancelled", "skipped"):
            mark = "-"
        else:
            mark = " "
        ident = str(item.get("id") or "").strip()
        prefix = f"{ident}: " if ident else ""
        lines.append(f"- [{mark}] {prefix}{content}")
    return "\n".join(lines) if lines else "- [ ] (empty plan)"


def handle_todo_write_tool(args: dict[str, Any], context: Any) -> ToolResult:
    from coderai.tools.legacy.types import ToolResult

    todos = args.get("todos")
    if not isinstance(todos, list) or not todos:
        return ToolResult(ok=False, name="todo_write", error="todos must be a non-empty array.")
    normalized: list[dict[str, Any]] = []
    for item in todos:
        if not isinstance(item, dict):
            return ToolResult(ok=False, name="todo_write", error="each todo must be an object.")
        content = item.get("content") or item.get("title")
        if not isinstance(content, str) or not content.strip():
            return ToolResult(ok=False, name="todo_write", error="each todo needs content.")
        status = str(item.get("status") or "pending").strip().lower()
        if status == "done":
            status = "completed"
        if status not in VALID_TODO_STATUS:
            return ToolResult(
                ok=False,
                name="todo_write",
                error=f"invalid todo status '{status}'. Allowed: {list(VALID_TODO_STATUS)}",
            )
        normalized.append(
            {
                "id": str(item.get("id") or ""),
                "content": content.strip(),
                "status": status,
            }
        )
    plan = todos_to_plan(normalized)
    result = handle_update_plan_tool(
        {"plan": plan, "explanation": args.get("merge") and "todo_write"}, context
    )
    result.name = "todo_write"
    meta = dict(result.metadata or {})
    meta["todos"] = normalized
    result.metadata = meta
    if result.ok:
        result.output = "Todos updated."
        # Phase 2: persist todos into SessionState (state.todos) so
        # resume/compaction survivors keep the checklist. Best-effort.
        try:
            session_id = None
            mgr = None
            if isinstance(context, dict):
                session_id = context.get("session_id")
            else:
                session_id = getattr(context, "session_id", None)
                mgr = getattr(context, "manager", None) or getattr(context, "session_manager", None)
            if mgr is not None and session_id:
                from coderai.session_state import TodoItemState

                state = mgr.get_session_state(session_id)
                state.todos = [
                    TodoItemState(
                        title=str(t.get("content", ""))[:500],
                        status="in_progress"
                        if t.get("status") == "in_progress"
                        else ("done" if t.get("status") in ("completed", "done") else "pending"),
                    )
                    for t in normalized
                ]
                mgr._save_session_state(session_id)
        except Exception:
            pass
    return result
