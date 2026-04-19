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
    checkpoints: Dict[str, Any] = Field(default_factory=dict)

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

    def create_checkpoint(self, label: str = None) -> str:
        """Create a checkpoint of the current conversation state.

        Args:
            label: Optional human-readable label

        Returns:
            Checkpoint ID
        """
        cp_id = f"cp_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        self.checkpoints[cp_id] = {
            "messages": [msg.model_dump() for msg in self.messages],
            "label": label or f"Checkpoint at {len(self.messages)} messages",
            "created_at": time.time(),
            "message_count": len(self.messages),
        }
        self.updated_at = time.time()
        return cp_id

    def rollback(self, checkpoint_id: str) -> bool:
        """Rollback the session to a checkpoint.

        Args:
            checkpoint_id: ID of the checkpoint to rollback to

        Returns:
            True if rollback succeeded
        """
        if checkpoint_id not in self.checkpoints:
            return False
        cp = self.checkpoints[checkpoint_id]
        self.messages = [Message(**m) for m in cp["messages"]]
        self.updated_at = time.time()
        return True

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        """List all available checkpoints."""
        result = []
        for cp_id, cp in self.checkpoints.items():
            result.append({
                "id": cp_id,
                "label": cp["label"],
                "message_count": cp["message_count"],
                "created_at": datetime.fromtimestamp(cp["created_at"]).strftime("%H:%M:%S"),
            })
        return result



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
            
        self._update_index(session)

    def _update_index(self, session: Session) -> None:
        """Update the fast-lookup index for a session."""
        index_file = self.history_dir / "index.json"
        index = {}
        if index_file.exists():
            try:
                with open(index_file, "r") as f:
                    index = json.load(f)
            except Exception:
                pass
                
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
            import os
            os.replace(tmp_file, index_file)
        except Exception as e:
            logger.warning(f"Failed to update session index: {e}")

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all available sessions using index.json cache."""
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
                import os
                os.replace(tmp_file, index_file)
            except Exception:
                pass

        sessions = list(index.values())
        sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
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

    def save_session_summary(self, session_id: str, summary: str) -> None:
        """Save a session summary for quick lookup."""
        if not _SESSION_ID_PATTERN.match(session_id):
            return

        summary_file = self.history_dir / f"{session_id}_summary.json"
        data = {
            "session_id": session_id,
            "summary": summary,
            "saved_at": datetime.now().isoformat(),
        }
        with open(summary_file, "w") as f:
            json.dump(data, f, indent=2)

    def search_summaries(self, query: str) -> List[Dict[str, Any]]:
        """Search session summaries by keyword."""
        query_lower = query.lower()
        results = []
        for summary_file in self.history_dir.glob("*_summary.json"):
            try:
                with open(summary_file, "r") as f:
                    data = json.load(f)
                if query_lower in data.get("summary", "").lower():
                    results.append(data)
            except Exception:
                continue
        return results

    def get_session_summary(self, session_id: str) -> Optional[str]:
        """Get a session's summary, if one was saved."""
        if not _SESSION_ID_PATTERN.match(session_id):
            return None
        summary_file = self.history_dir / f"{session_id}_summary.json"
        if summary_file.exists():
            try:
                with open(summary_file, "r") as f:
                    data = json.load(f)
                return data.get("summary")
            except Exception:
                return None
        return None


# Global history manager instance
history_manager = HistoryManager()

