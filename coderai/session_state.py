# Ported from coderai/core/session_state.py - kimi structure (session_state.py).
"""Per-session persisted state (Kimi ``session_state.py`` parity).

``SessionState`` lives at ``<project>/.coderai/sessions/<id>/state.json`` —
alongside (not inside) the JSONL log. It owns approval flags, workspace
scope, title bookkeeping, plan-mode persistence, and the todo list, so a
resumed session restores behavior, not just history.

Writes use :func:`coderai.core.common.atomic.atomic_json_write`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from coderai.core.common.atomic import atomic_json_write
from coderai.core.log import logger

STATE_FILE_NAME = "state.json"


class ApprovalStateData(BaseModel):
    yolo: bool = False
    afk: bool = False
    auto_approve_actions: set[str] = Field(default_factory=set)


class TodoItemState(BaseModel):
    """A single todo item stored in session or subagent state."""

    title: str
    status: Literal["pending", "in_progress", "done"]


class SessionState(BaseModel):
    version: int = 1
    approval: ApprovalStateData = Field(default_factory=ApprovalStateData)
    additional_dirs: list[str] = Field(default_factory=list)
    custom_title: str | None = None
    title_generated: bool = False
    title_generate_attempts: int = 0
    plan_mode: bool = False
    plan_session_id: str | None = None
    plan_slug: str | None = None
    wire_mtime: float | None = None
    archived: bool = False
    archived_at: float | None = None
    auto_archive_exempt: bool = False
    todos: list[TodoItemState] = Field(default_factory=list)


def session_state_path(session_dir: Path) -> Path:
    """Absolute path of ``state.json`` for a session directory."""
    return Path(session_dir) / STATE_FILE_NAME


def load_session_state(session_dir: Path) -> SessionState:
    """Load state (defaults on missing/corrupt file; never raises)."""
    state_file = session_state_path(session_dir)
    if not state_file.exists():
        return SessionState()
    try:
        with open(state_file, encoding="utf-8") as f:
            return SessionState.model_validate(json.load(f))
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError, OSError) as e:
        logger.warning("Corrupted state file, using defaults: {path}", path=str(state_file))
        _ = e
        return SessionState()


def save_session_state(state: SessionState, session_dir: Path) -> None:
    """Persist state atomically (creating parent dirs)."""
    atomic_json_write(state.model_dump(mode="json"), session_state_path(session_dir))
