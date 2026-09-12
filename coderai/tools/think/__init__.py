"""Think tool.

Appends a reasoning note to the session log without performing any I/O.
Use when complex reasoning or scratch memory is needed.
"""

from __future__ import annotations

from typing import Any

from coderai.tools.legacy.types import ToolResult


def handle_think_tool(args: dict[str, Any], context: Any) -> ToolResult:
    thought = args.get("thought")
    if not isinstance(thought, str) or not thought.strip():
        return ToolResult(ok=False, name="Think", error="thought must be a non-empty string.")
    thought = thought.strip()
    # Persist into session transcript as a thinking entry when possible.
    try:
        session_id = getattr(context, "session_id", None) or getattr(context, "sid", None)
        mgr = getattr(context, "manager", None) or getattr(context, "session_manager", None)
        if mgr is not None and session_id:
            mgr._append_message(
                mgr._build_message(
                    session_id,
                    "assistant",
                    f"[thought] {thought[:4000]}",
                    meta={"isThought": True},
                )
            )
    except Exception:
        pass
    return ToolResult(
        ok=True,
        name="Think",
        output=f"Thought recorded ({len(thought)} chars).",
    )
