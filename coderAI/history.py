"""Conversation history management for CoderAI."""

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Message(BaseModel):
    """A single message in the conversation."""

    role: str  # 'user', 'assistant', 'system', 'tool'
    content: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # Tool name for tool messages


def _default_session_model() -> str:
    """Resolve the configured default model lazily.

    Imported inside the function to avoid a circular import during module
    load (``config`` imports pydantic, and ``history`` is imported early by
    ``agent``).
    """
    try:
        from .config import config_manager
        return config_manager.load().default_model
    except Exception:
        # Fall back to the Config field default rather than an invalid literal.
        try:
            from .config import Config
            return Config.model_fields["default_model"].default
        except Exception:
            return "claude-4-sonnet"


class Session(BaseModel):
    """A conversation session."""

    session_id: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    messages: List[Message] = Field(default_factory=list)
    model: str = Field(default_factory=_default_session_model)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def add_message(self, role: str, content: str, **kwargs) -> None:
        """Add a message to the session."""
        message = Message(role=role, content=content, **kwargs)
        self.messages.append(message)
        self.updated_at = time.time()

    def get_messages_for_api(self) -> List[Dict[str, Any]]:
        """Get messages in OpenAI API format."""
        api_messages = []
        for msg in self.messages:
            msg_dict = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                msg_dict["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            if msg.name:
                msg_dict["name"] = msg.name
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

        # Nullify content when tool_calls are present (providers reject mixed messages)
        if role == "assistant" and msg.get("tool_calls") and msg.get("content"):
            logger.warning(
                "Nullifying content on assistant message with tool_calls in session %s",
                data.get("session_id"),
            )
            msg["content"] = None

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
                            logger.warning("Dropping malformed stored tool arguments in session %s", data.get("session_id"))
                            continue
                        if not isinstance(parsed, dict):
                            logger.warning("Dropping non-object stored tool arguments in session %s", data.get("session_id"))
                            continue
                    fn_copy["arguments"] = parsed
                    tc_copy["function"] = fn_copy
                tc_id = tc_copy.get("id")
                if isinstance(tc_id, str) and tc_id:
                    seen_tool_ids.add(tc_id)
                clean_tool_calls.append(tc_copy)
            msg["tool_calls"] = clean_tool_calls or None

        if msg.get("role") == "tool":
            tool_call_id = msg.get("tool_call_id")
            if tool_call_id and tool_call_id not in seen_tool_ids:
                logger.warning("Dropping orphaned tool_result %s from session %s", tool_call_id, data.get("session_id"))
                continue

        sanitized_messages.append(msg)

    data = dict(data)
    data["messages"] = sanitized_messages
    return data


class HistoryManager:
    """Manages conversation history."""

    def __init__(self):
        """Initialize the history manager."""
        self.history_dir = Path.home() / ".coderAI" / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.current_session: Optional[Session] = None

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

        with open(session_file, "r") as f:
            data = _sanitize_session_data(json.load(f))
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

        session_file = self.history_dir / f"{session.session_id}.json"
        tmp_file = session_file.with_suffix(".json.tmp")
        try:
            with open(tmp_file, "w") as f:
                json.dump(session.model_dump(), f, indent=2)
            os.replace(tmp_file, session_file)
        except Exception:
            # Don't leave an orphan ``.tmp`` when the write itself failed.
            try:
                tmp_file.unlink()
            except OSError:
                pass
            raise

        self._update_index(session)

    def _update_index(self, session: Session) -> None:
        """Update the fast-lookup index for a session."""
        index_file = self.history_dir / "index.json"
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
                logger.warning(
                    "Could not read session index %s: %s", index_file, e
                )
                
        index[session.session_id] = {
            "session_id": session.session_id,
            "created_at": datetime.fromtimestamp(session.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.fromtimestamp(session.updated_at).strftime("%Y-%m-%d %H:%M:%S"),
            "messages": len(session.messages),
            "model": session.model,
        }
        try:
            tmp_file = index_file.with_suffix('.json.tmp')
            with open(tmp_file, "w") as f:
                json.dump(index, f)
            os.replace(tmp_file, index_file)
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
                            "created_at": datetime.fromtimestamp(data.get("created_at", time.time())).strftime("%Y-%m-%d %H:%M:%S"),
                            "updated_at": datetime.fromtimestamp(data.get("updated_at", time.time())).strftime("%Y-%m-%d %H:%M:%S"),
                            "messages": len(data.get("messages", [])),
                            "model": data.get("model", "unknown"),
                        }
                        needs_save = True
                except Exception:
                    continue
                    
        if needs_save:
            try:
                tmp_file = index_file.with_suffix('.json.tmp')
                with open(tmp_file, "w") as f:
                    json.dump(index, f)
                os.replace(tmp_file, index_file)
            except Exception:
                pass

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
        try:
            with open(index_file, "r") as f:
                index = json.load(f)
            if session_id in index:
                del index[session_id]
                tmp_file = index_file.with_suffix('.json.tmp')
                with open(tmp_file, "w") as f:
                    json.dump(index, f)
                os.replace(tmp_file, index_file)
        except Exception as e:
            logger.warning(f"Failed to update index after session delete: {e}")

    def _cleanup_expired_sessions(self) -> None:
        """Delete sessions older than the retention window."""
        cutoff = time.time() - _SESSION_RETENTION_SECONDS
        removed_ids = []
        for session_file in self.history_dir.glob("session_*.json"):
            try:
                if session_file.stat().st_mtime < cutoff:
                    session_id = session_file.stem.replace("session_", "")
                    session_file.unlink()
                    removed_ids.append(session_id)
            except OSError:
                continue
        for sid in removed_ids:
            self._remove_from_index(sid)


# Global history manager instance
history_manager = HistoryManager()
