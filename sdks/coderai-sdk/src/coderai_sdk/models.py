"""Plain data types for the headless CoderAI SDK.

Stdlib only so the types are importable without the CoderAI engine installed;
the live engine is only needed when a client actually drives a turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatMessage:
    """A single collected chat message (assistant or tool output)."""

    role: str
    content: str = ""


@dataclass
class SessionInfo:
    """Identifying snapshot of a headless session."""

    session_id: str
    status: str = ""
    summary: str = ""


@dataclass
class TurnResult:
    """Collected outcome of one headless :meth:`CoderAIClient.prompt` turn."""

    session_id: str | None = None
    text: str = ""
    thinking: str = ""
    messages: list[ChatMessage] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)


__all__ = ["ChatMessage", "SessionInfo", "TurnResult"]
