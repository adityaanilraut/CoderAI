"""Session approval and repetition sanitization helpers."""

from __future__ import annotations

import re
from typing import Any

# Global active session managers registry for tool-layer inspection
_session_managers: list[Any] = []


def register_session_manager(manager: Any) -> None:
    """Register a SessionManager instance in the global active list."""
    if manager not in _session_managers:
        _session_managers.append(manager)


def unregister_session_manager(manager: Any) -> None:
    """Unregister a SessionManager instance."""
    try:
        _session_managers.remove(manager)
    except ValueError:
        pass


def sanitize_repetition_loops(text: str) -> str:
    """Detect and collapse pathological token repetition loops in model output."""
    if not text or len(text) < 40:
        return text
    pattern = re.compile(r"(.{6,150}?)(?:\s*\1){3,}", re.DOTALL)

    def _replace(match: re.Match) -> str:
        unit = match.group(1).strip()
        return f"{unit} [truncated repetition loop]"

    return pattern.sub(_replace, text)


def global_afk_check() -> bool:
    """Check if any active session manager has AFK or YOLO enabled."""
    for m in _session_managers:
        try:
            if m.is_afk() or m.is_yolo():
                return True
        except Exception:
            continue
    return False


def check_afk_for_session(session_id: str) -> bool:
    """Check if AFK or YOLO is enabled for a given session ID."""
    for m in _session_managers:
        try:
            if session_id in getattr(m, "session_controllers", {}) or session_id == getattr(
                m, "_active_session_id", None
            ):
                if m.is_afk() or m.is_yolo():
                    return True
        except Exception:
            continue
    return False
