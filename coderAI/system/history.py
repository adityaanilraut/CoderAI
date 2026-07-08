"""Conversation history management for CoderAI."""

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coderAI.system.fsperms import OWNER_RWX, atomic_write_json, restrict_path

logger = logging.getLogger(__name__)


def _atomic_write_json(target: Path, obj: Any) -> None:
    """Atomically write *obj* as compact, owner-only (0600) JSON to *target*.

    Session files hold conversation content and the index must never be left
    truncated by a crash; :func:`atomic_write_json` guarantees both. Compact
    output (``indent=None``) keeps potentially large session files small.
    """
    atomic_write_json(target, obj, indent=None)


class Message(BaseModel):
    """A single message in the conversation."""

    role: str  # 'user', 'assistant', 'system', 'tool'
    content: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # Tool name for tool messages
    reasoning_content: Optional[str] = None
    # Base64 images attached to a tool result (e.g. from ``read_image``).
    # Each entry is ``{"mime_type": str, "data": <base64>}``. Providers that
    # support vision render these as real image blocks; the heavy base64 is
    # kept out of the text ``content`` so it survives result summarization.
    tool_images: Optional[List[Dict[str, Any]]] = None


def _default_session_model() -> str:
    """Resolve the configured default model lazily.

    Imported inside the function to avoid a circular import during module
    load (``config`` imports pydantic, and ``history`` is imported early by
    ``agent``).
    """
    try:
        from coderAI.system.config import config_manager

        return config_manager.load().default_model
    except Exception:
        # Fall back to the Config field default rather than an invalid literal.
        logger.debug("default_model config unavailable, using field default", exc_info=True)
        try:
            from coderAI.system.config import Config

            return str(Config.model_fields["default_model"].default)
        except Exception:
            # Last resort if even the Config class can't be imported/inspected.
            return "claude-4-sonnet"


def checkpoint_label(text: Optional[str]) -> str:
    """Build a short single-line preview of a user turn for a rewind point.

    Shared by the checkpoint recorder (``Agent._record_checkpoint``) and the
    ``/rewind`` turn listing so the stored and displayed labels can't drift.
    """
    stripped = (text or "").strip().splitlines()
    return stripped[0][:60] if stripped else "(empty)"


class Checkpoint(BaseModel):
    """A conversation rewind point captured at the start of a user turn.

    ``message_index`` is ``len(session.messages)`` at the moment just before
    the turn's messages (skill injections + the user message + the assistant
    reply) were appended, so truncating ``messages`` to it removes the whole
    turn. ``created_at`` is the cutoff used to revert file edits made since.
    """

    turn: int  # 1-based user-turn number
    label: str  # short preview of the user message
    message_index: int
    created_at: float = Field(default_factory=time.time)


class Session(BaseModel):
    """A conversation session."""

    session_id: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    messages: List[Message] = Field(default_factory=list)
    model: str = Field(default_factory=_default_session_model)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    checkpoints: List[Checkpoint] = Field(default_factory=list)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        message = Message(role=role, content=content, **kwargs)
        self.messages.append(message)
        self.updated_at = time.time()

    def add_checkpoint(self, label: str) -> Checkpoint:
        """Record a rewind point for the turn that is about to start."""
        checkpoint = Checkpoint(
            turn=len(self.checkpoints) + 1,
            label=label,
            message_index=len(self.messages),
        )
        self.checkpoints.append(checkpoint)
        self.updated_at = time.time()
        return checkpoint

    def truncate_to_checkpoint(self, turn: int) -> Optional[Checkpoint]:
        """Rewind history to before ``turn``.

        Truncates ``messages`` to the checkpoint's ``message_index`` and drops
        that checkpoint and every later one. Returns the matched checkpoint
        (so callers can read its ``created_at`` cutoff), or ``None`` if no
        checkpoint has that turn number.
        """
        target = next((c for c in self.checkpoints if c.turn == turn), None)
        if target is None:
            return None
        self.messages = self.messages[: target.message_index]
        self.checkpoints = [c for c in self.checkpoints if c.turn < turn]
        self.updated_at = time.time()
        return target

    def get_messages_for_api(self) -> List[Dict[str, Any]]:
        """Get messages in OpenAI API format."""
        api_messages = []
        for msg in self.messages:
            msg_dict: Dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                msg_dict["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            if msg.name:
                msg_dict["name"] = msg.name
            if msg.reasoning_content:
                msg_dict["reasoning_content"] = msg.reasoning_content
            if msg.tool_images:
                msg_dict["tool_images"] = msg.tool_images
            api_messages.append(msg_dict)
        return api_messages


# Valid session ID pattern: session_<timestamp>_<hex8>
_SESSION_ID_PATTERN = re.compile(r"^session_\d+_[a-f0-9]{8}$")
_SESSION_RETENTION_SECONDS = 30 * 24 * 60 * 60


_VALID_ROLES = {"system", "user", "assistant", "tool"}


def _sanitize_session_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop or repair malformed messages before loading a session."""
    messages = data.get("messages", []) or []
    sanitized_messages = []
    seen_tool_ids = set()

    for raw in messages:
        if not isinstance(raw, dict):
            continue
        msg = dict(raw)

        # Validate role
        role = msg.get("role")
        if not isinstance(role, str) or role not in _VALID_ROLES:
            logger.warning(
                "Dropping message with missing/invalid role %r in session %s",
                role,
                data.get("session_id"),
            )
            continue

        # Only assistant may emit tool_calls
        if role != "assistant" and msg.get("tool_calls"):
            logger.warning(
                "Stripping tool_calls from non-assistant %r message in session %s",
                role,
                data.get("session_id"),
            )
            msg.pop("tool_calls", None)

        # Validate timestamp type
        ts = msg.get("timestamp")
        if ts is not None and not isinstance(ts, (int, float)):
            logger.warning(
                "Dropping non-numeric timestamp %r in session %s",
                ts,
                data.get("session_id"),
            )
            msg.pop("timestamp", None)

        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            clean_tool_calls = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_copy = dict(tc)
                fn = tc_copy.get("function")
                if isinstance(fn, dict):
                    fn_copy = dict(fn)
                    args = fn_copy.get("arguments")
                    if isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                        except json.JSONDecodeError:
                            logger.warning(
                                "Dropping malformed stored tool arguments in session %s",
                                data.get("session_id"),
                            )
                            continue
                        if not isinstance(parsed, dict):
                            logger.warning(
                                "Dropping non-object stored tool arguments in session %s",
                                data.get("session_id"),
                            )
                            continue
                        fn_copy["arguments"] = json.dumps(parsed)
                    elif isinstance(args, dict):
                        fn_copy["arguments"] = json.dumps(args)
                    elif args is None:
                        fn_copy["arguments"] = "{}"
                    else:
                        logger.warning(
                            "Dropping tool call with invalid argument type %s in session %s",
                            type(args).__name__,
                            data.get("session_id"),
                        )
                        continue
                    tc_copy["function"] = fn_copy
                tc_id = tc_copy.get("id")
                if isinstance(tc_id, str) and tc_id:
                    seen_tool_ids.add(tc_id)
                clean_tool_calls.append(tc_copy)
            msg["tool_calls"] = clean_tool_calls or None

        if msg.get("role") == "tool":
            tool_call_id = msg.get("tool_call_id")
            if tool_call_id and tool_call_id not in seen_tool_ids:
                logger.warning(
                    "Dropping orphaned tool_result %s from session %s",
                    tool_call_id,
                    data.get("session_id"),
                )
                continue

        sanitized_messages.append(msg)

    data = dict(data)
    data["messages"] = sanitized_messages
    return data


class HistoryManager:
    """Manages conversation history."""

    # Expired-session cleanup scans (glob + stat) the whole history directory,
    # so it must not run on every save/load/list. Run it at most this often.
    _CLEANUP_INTERVAL_SECONDS = 3600.0

    def __init__(self) -> None:
        """Initialize the history manager."""
        self.history_dir = Path.home() / ".coderAI" / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        restrict_path(self.history_dir, OWNER_RWX)
        self.current_session: Optional[Session] = None
        self._index_lock = threading.Lock()
        # Throttle clock for ``_cleanup_expired_sessions``; ``-inf`` forces the
        # first call (e.g. the first ``list_sessions`` after launch) to run.
        self._last_cleanup_ts = float("-inf")
        self._cleanup_lock = threading.Lock()

    def create_session(self, model: Optional[str] = None) -> Session:
        """Create a new session, defaulting to the configured model."""
        self._cleanup_expired_sessions()
        session_id = f"session_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        if model is None:
            model = _default_session_model()
        self.current_session = Session(session_id=session_id, model=model)
        return self.current_session

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load a session from disk."""
        self._cleanup_expired_sessions()
        if not _SESSION_ID_PATTERN.match(session_id):
            return None

        session_file = self.history_dir / f"{session_id}.json"
        if not session_file.exists():
            return None

        try:
            with open(session_file, "r") as f:
                data = _sanitize_session_data(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load session %s: %s", session_id, e)
            return None
        session = Session(**data)
        self.current_session = session
        return session

    def save_session(self, session: Optional[Session] = None) -> None:
        """Save a session to disk.

        Writes via a temp file + ``os.replace`` so a crash mid-write cannot
        leave a truncated/invalid JSON behind that breaks ``list_sessions()``
        on the next launch. Same atomicity pattern the index already uses.
        """
        self._cleanup_expired_sessions()
        if session is None:
            session = self.current_session
        if session is None:
            return
        # ``model_dump()`` snapshots the live session on the caller's thread so
        # the dict handed to ``save_session_data`` (which callers may offload to
        # a background thread) can never race with in-loop session mutations.
        self.save_session_data(session.model_dump(), run_cleanup=False)

    def save_session_data(self, data: Dict[str, Any], *, run_cleanup: bool = True) -> None:
        """Write a pre-serialized session dict to disk.

        Split out from :meth:`save_session` so the expensive disk I/O can be
        offloaded to a background thread (see ``Agent.save_session``) while the
        ``model_dump()`` snapshot stays on the calling thread. ``data`` must be
        the output of ``Session.model_dump()``.

        Writes via a unique temp file + ``os.replace`` so a crash mid-write
        cannot leave a truncated/invalid JSON behind, and concurrent writers
        can't clobber each other's temp file.
        """
        if run_cleanup:
            self._cleanup_expired_sessions()
        session_id = data.get("session_id")
        if not session_id:
            return

        session_file = self.history_dir / f"{session_id}.json"
        _atomic_write_json(session_file, data)

        self._update_index(data)

    def _update_index(self, data: Dict[str, Any]) -> None:
        """Update the fast-lookup index from a session dict."""
        session_id = data.get("session_id")
        if not session_id:
            return
        index_file = self.history_dir / "index.json"
        with self._index_lock:
            index = {}
            if index_file.exists():
                try:
                    with open(index_file, "r") as f:
                        index = json.load(f)
                except json.JSONDecodeError:
                    logger.warning(
                        "Session index %s is corrupted, rebuilding from session files.",
                        index_file,
                    )
                except OSError as e:
                    logger.warning("Could not read session index %s: %s", index_file, e)

            index[session_id] = {
                "session_id": session_id,
                "created_at": datetime.fromtimestamp(data.get("created_at", time.time())).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "updated_at": datetime.fromtimestamp(data.get("updated_at", time.time())).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "messages": len(data.get("messages", [])),
                "model": data.get("model", "unknown"),
            }
            try:
                _atomic_write_json(index_file, index)
            except Exception as e:
                logger.warning(f"Failed to update session index: {e}")

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all available sessions using index.json cache."""
        self._cleanup_expired_sessions()
        index_file = self.history_dir / "index.json"
        index = {}
        if index_file.exists():
            try:
                with open(index_file, "r") as f:
                    index = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read session index, rebuilding: {e}")

        session_files = list(self.history_dir.glob("session_*.json"))
        valid_ids = {f.stem for f in session_files}

        needs_save = False

        # Clean deleted
        for sid in list(index.keys()):
            if sid not in valid_ids:
                del index[sid]
                needs_save = True

        # Rebuild missing
        for session_file in session_files:
            sid = session_file.stem
            if sid not in index:
                try:
                    with open(session_file, "r") as f:
                        data = json.load(f)
                        index[sid] = {
                            "session_id": data.get("session_id", sid),
                            "created_at": datetime.fromtimestamp(
                                data.get("created_at", time.time())
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                            "updated_at": datetime.fromtimestamp(
                                data.get("updated_at", time.time())
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                            "messages": len(data.get("messages", [])),
                            "model": data.get("model", "unknown"),
                        }
                        needs_save = True
                except Exception:
                    # A corrupt session file shouldn't break listing the rest;
                    # it stays out of the index until repaired or deleted.
                    logger.debug(f"skipping unreadable session file {session_file}", exc_info=True)
                    continue

        if needs_save:
            try:
                _atomic_write_json(index_file, index)
            except Exception as e:
                # The index is a cache; listing still works from the rebuilt
                # in-memory copy and the save is retried on the next call.
                logger.warning(f"Failed to save rebuilt session index: {e}")

        sessions = list(index.values())
        # updated_at is formatted YYYY-MM-DD HH:MM:SS which sorts lexicographically in chronological order
        sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return sessions

    def get_latest_session_id(self) -> Optional[str]:
        """Return the most recently updated session id, or None."""
        sessions = self.list_sessions()
        return sessions[0]["session_id"] if sessions else None

    def clear_history(self) -> int:
        """Clear all history. Returns number of sessions deleted."""
        count = 0
        with self._index_lock:
            for session_file in self.history_dir.glob("session_*.json"):
                session_file.unlink()
                count += 1
            # Remove the index so list_sessions starts fresh
            index_file = self.history_dir / "index.json"
            if index_file.exists():
                index_file.unlink()
        return count

    def delete_session(self, session_id: str) -> bool:
        """Delete a specific session."""
        if not _SESSION_ID_PATTERN.match(session_id):
            return False

        session_file = self.history_dir / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()
            # Remove from index
            self._remove_from_index(session_id)
            return True
        return False

    def _remove_from_index(self, session_id: str) -> None:
        """Remove a session from the fast-lookup index."""
        index_file = self.history_dir / "index.json"
        if not index_file.exists():
            return
        with self._index_lock:
            try:
                with open(index_file, "r") as f:
                    index = json.load(f)
                if session_id in index:
                    del index[session_id]
                    _atomic_write_json(index_file, index)
            except Exception as e:
                logger.warning(f"Failed to update index after session delete: {e}")

    def _cleanup_expired_sessions(self, *, force: bool = False) -> None:
        """Delete sessions older than the retention window.

        This globs and ``stat()``s every session file, so the cost grows with
        the history size. It is called from ``save_session``/``load_session``/
        ``create_session``/``list_sessions`` — several times per turn — so the
        scan is throttled to once per ``_CLEANUP_INTERVAL_SECONDS`` unless
        ``force`` is set. Retention is a coarse, time-based eviction, so a
        slightly delayed sweep is harmless.
        """
        now = time.time()
        if not force:
            with self._cleanup_lock:
                if now - self._last_cleanup_ts < self._CLEANUP_INTERVAL_SECONDS:
                    return
                self._last_cleanup_ts = now
        else:
            with self._cleanup_lock:
                self._last_cleanup_ts = now

        cutoff = now - _SESSION_RETENTION_SECONDS
        removed_ids = []
        for session_file in self.history_dir.glob("session_*.json"):
            try:
                if session_file.stat().st_mtime < cutoff:
                    session_id = session_file.stem
                    session_file.unlink()
                    removed_ids.append(session_id)
            except OSError:
                continue
        if removed_ids:
            index_file = self.history_dir / "index.json"
            with self._index_lock:
                try:
                    if index_file.exists():
                        with open(index_file, "r") as f:
                            index = json.load(f)
                        for sid in removed_ids:
                            index.pop(sid, None)
                        _atomic_write_json(index_file, index)
                except Exception as e:
                    logger.warning(f"Failed to update index after cleanup: {e}")


# Global history manager instance
history_manager = HistoryManager()
