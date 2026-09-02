"""todo_write wraps UpdatePlan with a structured todo list."""

from __future__ import annotations

from typing import Any

from coderai.core.tools.types import ToolResult
from coderai.core.tools.update_plan import handle_update_plan_tool

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
    return result
