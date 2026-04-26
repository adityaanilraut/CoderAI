import pytest
from coderAI.history import HistoryManager
import time
import json
import uuid

@pytest.fixture
def temp_history(tmp_path):
    manager = HistoryManager()
    manager.history_dir = tmp_path / "history"
    manager.history_dir.mkdir(parents=True, exist_ok=True)
    return manager

def test_get_latest_session_id_empty(temp_history):
    assert temp_history.get_latest_session_id() is None

def test_get_latest_session_id_multiple(temp_history):
    # Create two sessions with different updated_at
    sid1 = f"session_1000_{uuid.uuid4().hex[:8]}"
    sid2 = f"session_2000_{uuid.uuid4().hex[:8]}"
    
    with open(temp_history.history_dir / f"{sid1}.json", "w") as f:
        json.dump({"session_id": sid1, "updated_at": 1000, "messages": [], "model": "claude"}, f)
        
    with open(temp_history.history_dir / f"{sid2}.json", "w") as f:
        json.dump({"session_id": sid2, "updated_at": 2000, "messages": [], "model": "claude"}, f)

    # list_sessions sorts by updated_at descending, so sid2 should be first
    assert temp_history.get_latest_session_id() == sid2


def test_load_session_drops_orphaned_tool_results(temp_history):
    sid = f"session_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    payload = {
        "session_id": sid,
        "updated_at": time.time(),
        "messages": [
            {"role": "tool", "content": '{"success": true}', "tool_call_id": "missing", "name": "read_file"}
        ],
        "model": "claude",
    }
    with open(temp_history.history_dir / f"{sid}.json", "w") as f:
        json.dump(payload, f)

    session = temp_history.load_session(sid)
    assert session is not None
    assert session.messages == []


def test_load_session_drops_malformed_tool_call_args(temp_history):
    sid = f"session_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    payload = {
        "session_id": sid,
        "updated_at": time.time(),
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":'},
                    }
                ],
            }
        ],
        "model": "claude",
    }
    with open(temp_history.history_dir / f"{sid}.json", "w") as f:
        json.dump(payload, f)

    session = temp_history.load_session(sid)
    assert session is not None
    assert session.messages[0].tool_calls is None
