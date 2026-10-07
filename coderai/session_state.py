"""Per-session persisted state.

``SessionState`` lives at ``<project>/.coderai/sessions/<id>/state.json`` —
alongside (not inside) the JSONL log. It owns approval flags, workspace
scope, title bookkeeping, plan-mode persistence, and the todo list, so a
resumed session restores behavior, not just history.

Writes use :func:`coderai.utils.io.atomic_json_write`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from coderai.utils.storage import read_bytes, write_bytes
from coderai.log import logger

STATE_FILE_NAME = "state.json"


class ApprovalStateData(BaseModel):
    yolo: bool = False
    afk: bool = False
    auto_approve: bool = False
    auto_approve_actions: set[str] = Field(default_factory=set)


class TodoItemState(BaseModel):
    """A single todo item stored in session or subagent state."""

    title: str
    id: str = ""
    status: Literal["pending", "in_progress", "done", "completed", "cancelled"]

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: Any) -> Any:
        return "completed" if value == "done" else value


class SessionState(BaseModel):
    version: int = 1
    approval: ApprovalStateData = Field(default_factory=ApprovalStateData)
    additional_dirs: list[str] = Field(default_factory=list)
    custom_title: str | None = None
    title_generated: bool = False
    title_generate_attempts: int = 0
    plan_mode: bool = False
    acp_model_key: str | None = None
    acp_thinking: bool | None = None
    plan_session_id: str | None = None
    plan_slug: str | None = None
    wire_mtime: float | None = None
    archived: bool = False
    archived_at: float | None = None
    auto_archive_exempt: bool = False
    todos: list[TodoItemState] = Field(default_factory=list)

    @field_validator("todos", mode="before")
    @classmethod
    def _validate_todos(cls, value: Any) -> list[TodoItemState]:
        if not isinstance(value, list):
            return []
        validated: list[TodoItemState] = []
        for item in value:
            if isinstance(item, TodoItemState):
                validated.append(item)
            elif isinstance(item, dict):
                validated.append(TodoItemState.model_validate(item))
        return validated


def session_state_path(session_dir: Path) -> Path:
    """Absolute path of ``state.json`` for a session directory."""
    return Path(session_dir) / STATE_FILE_NAME


def load_session_state(session_dir: Path) -> SessionState:
    """Load state (defaults on missing/corrupt file; never raises)."""
    state_file = session_state_path(session_dir)
    if not state_file.exists():
        return SessionState()
    try:
        return SessionState.model_validate(
            json.loads(read_bytes(state_file, limit=1_000_000).decode("utf-8"))
        )
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError, OSError, ValueError) as e:
        logger.warning("Corrupted state file, using defaults: {path}", path=str(state_file))
        _ = e
        return SessionState()


def save_session_state(state: SessionState, session_dir: Path) -> None:
    """Persist state atomically (creating parent dirs)."""
    if state.todos:
        state.todos = [
            t if isinstance(t, TodoItemState) else TodoItemState.model_validate(t)
            for t in state.todos
        ]
    write_bytes(
        session_state_path(session_dir),
        json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2).encode("utf-8"),
    )
