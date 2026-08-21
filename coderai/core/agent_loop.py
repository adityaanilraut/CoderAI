"""Bounded agent loop — port of dsh agent-loop with turn/step semantics.

Extracts the core iteration logic from ``SessionManager._activate()`` into a
dedicated class with explicit turn/step lifecycle events.  The ``SessionManager``
delegates to ``AgentLoop.run()`` instead of inlining the loop.

Turn/Step lifecycle::

    turn/start → step/start → derive_request → LLM call → response →
    [tool/call → tool/result]* → step/end → [turn-stopping check] → turn/end

Each turn may contain multiple steps (one LLM call + tool execution per step).
A turn ends when the model produces no tool calls (natural completion) or
when the model is interrupted/cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, TYPE_CHECKING

from coderai.core.events import (
    SessionEvent,
    make_turn_start,
    make_turn_end,
    make_step_start,
    make_step_end,
    make_assistant_event,
    make_tool_call_event,
    make_tool_result_event,
    make_request_header,
)

if TYPE_CHECKING:
    from coderai.core.session import SessionManager

logger = logging.getLogger(__name__)


class AgentLoop:
    """Bounded agent loop with explicit turn/step semantics.

    This class owns the control flow of a single activation (one ``reply``
    or ``create_session``).  It emits typed ``SessionEvent``s for each
    lifecycle transition while delegating storage, tool execution, and
    permission handling to the owning ``SessionManager``.

    Design goals (from dsh agent-loop):
    - Explicit turn/step boundaries with durable events
    - Pre-step compaction pressure check
    - Request header logged for reconstructability
    - Tool-call/result pairing enforced
    - Clean separation from session lifecycle management
    """

    def __init__(self, manager: SessionManager, session_id: str) -> None:
        self.manager = manager
        self.session_id = session_id
        self._turn = 0
        self._step = 0

    def _next_seq(self) -> int:
        return self.manager._next_seq(self.session_id)

    def _emit(self, event: SessionEvent) -> None:
        """Write a typed event to the session log."""
        self.manager._append_event(self.session_id, event)

    def emit_turn_start(self) -> None:
        self._turn += 1
        self._step = 0
        self._emit(make_turn_start(self._next_seq(), self._turn))

    def emit_turn_end(self, reason: str) -> None:
        self._emit(make_turn_end(self._next_seq(), self._turn, reason))

    def emit_step_start(self) -> None:
        self._step += 1
        self._emit(make_step_start(self._next_seq(), self._turn, self._step))

    def emit_step_end(self) -> None:
        self._emit(make_step_end(self._next_seq(), self._turn, self._step))

    def emit_request_header(self, model: str, system: str | None = None) -> None:
        header: dict[str, Any] = {"model": model}
        if system:
            header["system"] = system[:200]  # truncated for log compactness
        self._emit(
            make_request_header(self._next_seq(), header, reason="initial" if self._step == 1 else "change")
        )

    def emit_assistant(
        self,
        content: str,
        *,
        thinking: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> None:
        self._emit(
            make_assistant_event(
                self._next_seq(),
                self._turn,
                self._step,
                content=content,
                thinking=thinking,
                tool_calls=tool_calls,
                usage=usage,
                model=model,
            )
        )

    def emit_tool_call(self, call_id: str, name: str, arguments: str) -> None:
        self._emit(
            make_tool_call_event(
                self._next_seq(), self._turn, self._step, call_id, name, arguments
            )
        )

    def emit_tool_result(
        self,
        call_id: str,
        content: str,
        *,
        is_error: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._emit(
            make_tool_result_event(
                self._next_seq(),
                self._turn,
                self._step,
                call_id,
                content,
                is_error=is_error,
                meta=meta,
            )
        )

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def step(self) -> int:
        return self._step
