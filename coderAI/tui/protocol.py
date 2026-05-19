"""Chat event names (Python source of truth for the in-process UI)."""

from __future__ import annotations

AGENT_EVENT_NAMES = (
    "hello",
    "ready",
    "turn",
    "tool",
    "file_diff",
    "status",
    "agent",
    "session_patch",
    "available_models",
    "available_personas",
    "available_skills",
    "context_state",
    "info",
    "warning",
    "success",
    "error",
    "progress",
    "goodbye",
)
