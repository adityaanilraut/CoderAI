"""Per-turn mutable state shared by the execution loop and tool executor.

Phase 4.1: previously the loop and executor coordinated turn-scoped state by
reaching into ``Agent`` private attributes (``_assistant_reply_parts``,
``_turn_ingested_untrusted``) and by writing tracker fields directly. Those are
now owned by a single :class:`TurnContext`, created once per
``ExecutionLoop.run`` call (one per user message) and passed to
``ToolExecutor.orchestrate_tool_calls`` so both layers read and write the same
object instead of the agent's internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Optional

from coderAI.core.objective import ObjectiveState


# These operations manage or route the task but are not useful engineering
# work by themselves.  They can enable a later action, but must not stop the
# time-to-first-useful-action clock.
_NON_USEFUL_CONTROL_TOOLS = frozenset(
    {
        "internal_recovery",
        "manage_tasks",
        "request_plan_amendment",
        "submit_plan",
        "use_skill",
    }
)


@dataclass
class TurnContext:
    """Mutable per-turn state threaded through the iteration phases.

    Folds in the old ``_TurnState`` fields plus the two pieces of turn state
    that used to live on the ``Agent``:

    * ``reply_parts`` — assistant text accumulated across the turn's LLM rounds;
      joined by ``ExecutionLoop._finalize_turn`` to build the final response.
    * ``ingested_untrusted`` — the egress-gate taint flag (Phase 3.4). Flips
      true once the turn ingests ``UNTRUSTED_EXTERNAL`` tool output; the
      executor sets it and its egress gate reads it. Starts clean each turn
      because the object is created fresh.
    * ``ingested_untrusted_mcp`` — a narrower taint (Phase 7.3): true once the
      turn ingests output specifically from an MCP server. Arms the
      mutating-local-tool gate, which forces a human confirmation for a local
      mutation in an MCP-triggered turn *even under* ``auto_approve`` so a
      third-party server can't drive an unattended local write/exec.

    All fields carry defaults so a bare ``TurnContext()`` is valid — the
    executor holds one as a fallback for direct (test) invocation that does not
    supply a loop-owned turn.
    """

    user_message: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_schemas: Optional[list[dict[str, Any]]] = None
    hooks_data: Any = None
    max_iterations: int = 1
    iteration: int = 0
    consecutive_llm_errors: int = 0
    consecutive_tool_errors: int = 0
    consecutive_pauses: int = 0
    empty_post_tool_retries: int = 0
    tools_were_used: bool = False
    ingested_untrusted: bool = False
    ingested_untrusted_mcp: bool = False
    reply_parts: list[str] = field(default_factory=list)
    objective_state: Optional[ObjectiveState] = None
    # Monotonic objective clock and privacy-safe first-action observation.  The
    # event payload derived from these fields contains only a tool identifier
    # and elapsed milliseconds—never objective text, arguments, or results.
    objective_started_at: float = field(default_factory=time.monotonic)
    first_useful_action_elapsed_ms: Optional[int] = None
    routed_tool_names: set[str] = field(default_factory=set)
    # Capability warmth is objective-local. A fresh TurnContext is constructed
    # for every user objective, so successful schemas cannot leak into another
    # turn, session, or agent. The router still intersects these names with the
    # current permission/persona/Plan Mode eligible surface.
    warm_tool_names: set[str] = field(default_factory=set)

    def record_first_useful_action(
        self,
        tool_name: str,
        result: Any,
        *,
        executed: bool,
        now: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Record the first successful, relevant, real engineering action.

        Relevance is deliberately conservative: the tool must be present in
        the current objective's routed eligible schemas.  Routing/control-only
        operations, failures, denials, cached duplicates, and synthetic
        recovery replies do not qualify.  Returns the event-safe payload only
        for the first qualifying action.
        """
        if self.first_useful_action_elapsed_ms is not None or not executed:
            return None
        if tool_name in _NON_USEFUL_CONTROL_TOOLS or tool_name not in self.routed_tool_names:
            return None
        if not (isinstance(result, dict) and result.get("success") is True):
            return None
        observed_at = time.monotonic() if now is None else now
        elapsed_ms = max(0, round((observed_at - self.objective_started_at) * 1000))
        self.first_useful_action_elapsed_ms = elapsed_ms
        return {"tool_name": tool_name, "elapsed_ms": elapsed_ms}
