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
from typing import Any, Dict, List, Optional


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

    All fields carry defaults so a bare ``TurnContext()`` is valid — the
    executor holds one as a fallback for direct (test) invocation that does not
    supply a loop-owned turn.
    """

    user_message: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_schemas: Optional[List[Dict[str, Any]]] = None
    hooks_data: Any = None
    max_iterations: int = 1
    iteration: int = 0
    consecutive_llm_errors: int = 0
    consecutive_tool_errors: int = 0
    consecutive_pauses: int = 0
    tools_were_used: bool = False
    ingested_untrusted: bool = False
    reply_parts: List[str] = field(default_factory=list)
