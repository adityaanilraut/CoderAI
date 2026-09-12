"""Session facade for work directory lifecycle and persistence.

Provides:
- `Session` dataclass with work_dir, work_dir_meta, context_file, wire_file, state
- Session lifecycle methods: `create`, `find`, `list`, `list_all`, `continue_`, `delete`
- Backward-compatible access to CoderAI `SessionManager`, `SessionMessage`, `SessionEntry`
"""

from __future__ import annotations

import asyncio
import builtins
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kaos.path import KaosPath
from kosong.message import Message

from coderai.metadata import WorkDirMeta, load_metadata, save_metadata
from coderai.session_state import SessionState, load_session_state, save_session_state
from coderai.utils.logging import logger
from coderai.utils.string import shorten
from coderai.wire.file import WireFile
from coderai.wire.types import TurnBegin

# Backward-compat re-exports from core.session
from coderai.soul.session.manager import (
    SessionEntry,
    SessionManager,
    SessionMessage,
    get_project_code,
)


@dataclass(slots=True, kw_only=True)
class Session:
    """A session of a work directory."""

    # static metadata
    id: str
    """The session ID."""
    work_dir: KaosPath
    """The absolute path of the work directory."""
    work_dir_meta: WorkDirMeta
    """The metadata of the work directory."""
    context_file: Path
    """The absolute path to the file storing the message history."""
    wire_file: WireFile
    """The wire message log file wrapper."""

    # session state
    state: SessionState
    """Persisted session state (approval settings, plan mode, workspace scope, etc.)."""

    # refreshable metadata
    title: str
    """The title of the session."""
    updated_at: float
    """The timestamp of the last update to the session."""

    @property
    def dir(self) -> Path:
        """The absolute path of the session directory."""
        path = self.work_dir_meta.sessions_dir / self.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def subagents_dir(self) -> Path:
        """The absolute path of the subagent instances directory."""
        path = self.dir / "subagents"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def is_empty(self) -> bool:
        """Whether the session has any context history or a custom title."""
        if self.state.custom_title:
            return False
        if not self.wire_file.is_empty():
            return False
        try:
            with self.context_file.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    role = json.loads(line, strict=False).get("role")
                    if isinstance(role, str) and not role.startswith("_"):
                        return False
        except FileNotFoundError:
            return True
        except (OSError, ValueError, TypeError):
            logger.exception("Failed to read context file %s", self.context_file)
            return False
        return True

    def save_state(self) -> None:
        """Persist the session state to disk."""
        fresh = load_session_state(self.dir)
        self.state.custom_title = fresh.custom_title
        self.state.title_generated = fresh.title_generated
        self.state.title_generate_attempts = fresh.title_generate_attempts
        self.state.archived = fresh.archived
        self.state.archived_at = fresh.archived_at
        self.state.auto_archive_exempt = fresh.auto_archive_exempt
        save_session_state(self.state, self.dir)

    async def delete(self) -> None:
        """Delete the session directory."""
        session_dir = self.work_dir_meta.sessions_dir / self.id
        if not session_dir.exists():
            return
        await asyncio.to_thread(shutil.rmtree, session_dir, True)

    async def refresh(self) -> None:
        self.title = "Untitled"
        self.updated_at = self.context_file.stat().st_mtime if self.context_file.exists() else 0.0

        if self.state.custom_title:
            self.title = self.state.custom_title
            return

        try:
            async for record in self.wire_file.iter_records():
                wire_msg = record.to_wire_message()
                if isinstance(wire_msg, TurnBegin):
                    user_inp = wire_msg.user_input
                    if isinstance(user_inp, str):
                        self.title = shorten(user_inp, width=50)
                    elif isinstance(user_inp, list):
                        self.title = shorten(
                            Message(role="user", content=user_inp).extract_text(" "),
                            width=50,
                        )
                    return
        except Exception:
            pass

    @staticmethod
    async def create(
        work_dir: KaosPath | str,
        session_id: str | None = None,
        _context_file: Path | None = None,
    ) -> Session:
        """Create a new session for a work directory."""
        if isinstance(work_dir, str):
            work_dir = KaosPath(work_dir)
        work_dir = work_dir.canonical()

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            work_dir_meta = metadata.new_work_dir_meta(work_dir)

        if session_id is None:
            session_id = str(uuid.uuid4())
        session_dir = work_dir_meta.sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        if _context_file is None:
            context_file = session_dir / "context.jsonl"
        else:
            _context_file.parent.mkdir(parents=True, exist_ok=True)
            context_file = _context_file

        if context_file.exists():
            context_file.unlink()
        context_file.touch()

        work_dir_meta.last_session_id = session_id
        save_metadata(metadata)

        session = Session(
            id=session_id,
            work_dir=work_dir,
            work_dir_meta=work_dir_meta,
            context_file=context_file,
            wire_file=WireFile(path=session_dir / "wire.jsonl"),
            state=SessionState(),
            title="",
            updated_at=0.0,
        )
        await session.refresh()
        return session

    @staticmethod
    async def find(work_dir: KaosPath | str, session_id: str) -> Session | None:
        """Find a session by work directory and session ID."""
        if isinstance(work_dir, str):
            work_dir = KaosPath(work_dir)
        work_dir = work_dir.canonical()

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            return None

        _migrate_session_context_file(work_dir_meta, session_id)

        session_dir = work_dir_meta.sessions_dir / session_id
        if not session_dir.is_dir():
            return None

        context_file = session_dir / "context.jsonl"
        if not context_file.exists():
            return None

        session = Session(
            id=session_id,
            work_dir=work_dir,
            work_dir_meta=work_dir_meta,
            context_file=context_file,
            wire_file=WireFile(path=session_dir / "wire.jsonl"),
            state=load_session_state(session_dir),
            title="",
            updated_at=0.0,
        )
        await session.refresh()
        return session

    @staticmethod
    async def list(work_dir: KaosPath | str) -> builtins.list[Session]:
        """List all sessions for a work directory."""
        if isinstance(work_dir, str):
            work_dir = KaosPath(work_dir)
        work_dir = work_dir.canonical()

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            return []

        session_ids = {
            path.name if path.is_dir() else path.stem
            for path in work_dir_meta.sessions_dir.iterdir()
            if path.is_dir() or path.suffix == ".jsonl"
        }

        sessions: list[Session] = []
        for session_id in session_ids:
            _migrate_session_context_file(work_dir_meta, session_id)
            session_dir = work_dir_meta.sessions_dir / session_id
            if not session_dir.is_dir():
                continue
            context_file = session_dir / "context.jsonl"
            if not context_file.exists():
                continue
            session = Session(
                id=session_id,
                work_dir=work_dir,
                work_dir_meta=work_dir_meta,
                context_file=context_file,
                wire_file=WireFile(path=session_dir / "wire.jsonl"),
                state=load_session_state(session_dir),
                title="",
                updated_at=0.0,
            )
            if session.is_empty():
                continue
            await session.refresh()
            sessions.append(session)
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    @classmethod
    async def list_all(cls) -> builtins.list[Session]:
        """List sessions across all known work directories."""
        all_sessions: list[Session] = []
        for wd in load_metadata().work_dirs:
            sessions = await cls.list(KaosPath.unsafe_from_local_path(Path(wd.path)))
            all_sessions.extend(sessions)
        all_sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return all_sessions

    @staticmethod
    async def continue_(work_dir: KaosPath | str) -> Session | None:
        """Get the last session for a work directory."""
        if isinstance(work_dir, str):
            work_dir = KaosPath(work_dir)
        work_dir = work_dir.canonical()

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None or work_dir_meta.last_session_id is None:
            return None

        return await Session.find(work_dir, work_dir_meta.last_session_id)


def _migrate_session_context_file(work_dir_meta: WorkDirMeta, session_id: str) -> None:
    old_context_file = work_dir_meta.sessions_dir / f"{session_id}.jsonl"
    new_context_file = work_dir_meta.sessions_dir / session_id / "context.jsonl"
    if old_context_file.exists() and not new_context_file.exists():
        new_context_file.parent.mkdir(parents=True, exist_ok=True)
        old_context_file.rename(new_context_file)


__all__ = [
    "Session",
    "SessionManager",
    "SessionMessage",
    "SessionEntry",
    "get_project_code",
]
