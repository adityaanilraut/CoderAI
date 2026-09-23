"""Tool-execution context variables shared by the permission and ACP layers.

These helpers live in ``coderai.soul.tool_context``. Context vars remain because
the permission layer reads the active tool call to correlate an approval
request with the tool call that triggered it.

On the ``SessionManager`` engine approval requests fall back to a generated id
when no tool call is active.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from coderai.wire.types import ToolCall

current_tool_call: ContextVar[ToolCall | None] = ContextVar("current_tool_call", default=None)
_current_step_no: ContextVar[int | None] = ContextVar("current_step_no", default=None)
_current_session_id: ContextVar[str] = ContextVar("_current_session_id", default="")


def set_current_tool_call(tool_call: ToolCall | None) -> Token[ToolCall | None]:
    """Bind the active tool call for this task context; returns a reset token."""
    return current_tool_call.set(tool_call)


def get_current_tool_call_or_none() -> ToolCall | None:
    """Return the tool call bound to this task context, if any."""
    return current_tool_call.get()


def set_current_step_no(step_no: int) -> Token[int | None]:
    """Bind the active step number for this task context; returns a reset token."""
    return _current_step_no.set(step_no)


def get_current_step_no() -> int | None:
    """Return the step number associated with the current tool task."""
    return _current_step_no.get()


def set_session_id(session_id: str) -> None:
    _current_session_id.set(session_id)


def get_session_id() -> str:
    return _current_session_id.get()


__all__ = [
    "current_tool_call",
    "get_current_step_no",
    "get_current_tool_call_or_none",
    "get_session_id",
    "set_current_step_no",
    "set_current_tool_call",
    "set_session_id",
]
