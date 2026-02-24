"""Conversation history management for CoderAI."""

import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Message(BaseModel):
    """A single message in the conversation."""

    role: str  # 'user', 'assistant', 'system', 'tool'
    content: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # Tool name for tool messages


class Session(BaseModel):
    """A conversation session."""

    session_id: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    messages: List[Message] = Field(default_factory=list)
    model: str = "gpt-5-mini"
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


class HistoryManager:
    """Manages conversation history."""

    def __init__(self):
        """Initialize the history manager."""
        self.history_dir = Path.home() / ".coderAI" / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.current_session: Optional[Session] = None

    def create_session(self, model: str = "gpt-5-mini") -> Session:
        """Create a new session."""
        session_id = f"session_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        self.current_session = Session(session_id=session_id, model=model)
        return self.current_session

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load a session from disk."""
        if not _SESSION_ID_PATTERN.match(session_id):
            return None

        session_file = self.history_dir / f"{session_id}.json"
        if not session_file.exists():
            return None

        with open(session_file, "r") as f:
            data = json.load(f)
            session = Session(**data)
            self.current_session = session
            return session

    def save_session(self, session: Optional[Session] = None) -> None:
        """Save a session to disk."""
        if session is None:
            session = self.current_session
        if session is None:
            return

        session_file = self.history_dir / f"{session.session_id}.json"
        with open(session_file, "w") as f:
            json.dump(session.model_dump(), f, indent=2)

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all available sessions."""
        sessions = []
        for session_file in sorted(self.history_dir.glob("session_*.json"), reverse=True):
            try:
                with open(session_file, "r") as f:
                    data = json.load(f)
                    sessions.append(
                        {
                            "session_id": data["session_id"],
                            "created_at": datetime.fromtimestamp(
                                data["created_at"]
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                            "updated_at": datetime.fromtimestamp(
                                data["updated_at"]
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                            "messages": len(data["messages"]),
                            "model": data["model"],
                        }
                    )
            except Exception:
                continue
        return sessions

    def clear_history(self) -> int:
        """Clear all history. Returns number of sessions deleted."""
        count = 0
        for session_file in self.history_dir.glob("session_*.json"):
            session_file.unlink()
            count += 1
        return count

    def delete_session(self, session_id: str) -> bool:
        """Delete a specific session."""
        if not _SESSION_ID_PATTERN.match(session_id):
            return False

        session_file = self.history_dir / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()
            return True
        return False


# Global history manager instance
history_manager = HistoryManager()

