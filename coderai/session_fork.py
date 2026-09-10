# Ported from coderai/core/turns.py - kimi structure (session_fork.py).
"""Turn enumeration + history truncation (Kimi ``session_fork.py`` parity).

A *turn* is one user message plus everything until the next user message
(checkpoint/system markers excluded). These helpers power ``/undo``,
``/fork``, and the session-trace view. They operate on plain dict rows so
both the JSONL event log and in-memory messages work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CHECKPOINT_USER_PATTERN = re.compile(r"^<system>CHECKPOINT \d+</system>$")


@dataclass(frozen=True, slots=True)
class TurnInfo:
    """Summary of a single turn for display in the selector."""

    index: int
    """0-based turn index."""
    user_text: str
    """First-line text of the user message."""


def _row_role(row: dict[str, Any]) -> str:
    role = row.get("role")
    if isinstance(role, str) and role:
        return role
    data = row.get("data")
    if isinstance(data, dict) and isinstance(data.get("role"), str):
        return str(data["role"])
    msg = row.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("role"), str):
        return str(msg["role"])
    return ""


def _row_content(row: dict[str, Any]) -> str:
    for key in ("content", "text", "prompt"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    data = row.get("data")
    if isinstance(data, dict):
        for key in ("content", "text", "prompt"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def is_checkpoint_marker(row: dict[str, Any]) -> bool:
    """Whether a row is a synthetic ``<system>CHECKPOINT N</system>`` marker."""
    if _row_role(row) != "user":
        return False
    content = _row_content(row).strip()
    if CHECKPOINT_USER_PATTERN.fullmatch(content):
        return True
    meta = row.get("meta")
    if isinstance(meta, dict) and meta.get("isCheckpoint"):
        return True
    return False


def is_visible_user_turn(row: dict[str, Any]) -> bool:
    """Whether a row starts a new undoable user turn."""
    if _row_role(row) != "user":
        return False
    if is_checkpoint_marker(row):
        return False
    meta = row.get("meta")
    if isinstance(meta, dict):
        if meta.get("isHookDenial") or meta.get("isHookContext") or meta.get("isHookSystem"):
            return False
        if meta.get("isDMail"):
            return False
        if meta.get("isPlanMode") is not None and not _row_content(row).strip():
            return False
    return bool(_row_content(row).strip())


def first_line(text: str, limit: int = 80) -> str:
    """First non-empty line, truncated to ``limit`` chars."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit] if len(stripped) > limit else stripped
    return "(empty prompt)"


def enumerate_turns(rows: list[dict[str, Any]]) -> list[TurnInfo]:
    """Enumerate user turns in a row sequence (0-based, chronological)."""
    turns: list[TurnInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if is_visible_user_turn(row):
            turns.append(TurnInfo(index=len(turns), user_text=first_line(_row_content(row))))
    return turns


def truncate_rows_at_turn(rows: list[dict[str, Any]], turn_index: int) -> list[dict[str, Any]]:
    """Return rows up to and including ``turn_index`` (inclusive).

    Raises:
        ValueError: If ``turn_index`` is out of range.
    """
    if turn_index < 0:
        raise ValueError(f"turn_index {turn_index} out of range")
    current = -1
    kept: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict) and is_visible_user_turn(row):
            current += 1
            if current > turn_index:
                break
        kept.append(row)
    if current < turn_index:
        raise ValueError(f"turn_index {turn_index} out of range (max turn: {current})")
    return kept
