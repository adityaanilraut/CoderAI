"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools.session import (
    handle_session_event_read as _session_event_read,
    handle_session_event_search as _session_event_search,
    handle_session_search as _session_search,
    handle_session_trace as _session_trace,
)
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(
        define_tool(
            effects=builtin_effect_policy("session_search", False),
            name="session_search",
            description="Search past session titles, summaries, and replies by keyword.",
            parameters={"query": {"type": "string"}},
            required=["query"],
            handler=_session_search,
            aliases=["session_query"],
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("session_trace", False),
            name="session_trace",
            description="List recent events for one saved session.",
            parameters={"session_id": {"type": "string"}},
            required=["session_id"],
            handler=_session_trace,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("session_event_search", False),
            name="session_event_search",
            description="Search event text inside one saved session.",
            parameters={
                "session_id": {"type": "string"},
                "query": {"type": "string"},
            },
            required=["session_id", "query"],
            handler=_session_event_search,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("session_event_read", False),
            name="session_event_read",
            description="Read a slice of one session event log by offset.",
            parameters={
                "session_id": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            required=["session_id"],
            handler=_session_event_read,
            category="meta",
            is_mutating=False,
            is_concurrency_safe=True,
        )
    )
