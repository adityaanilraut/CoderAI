import pytest
from coderAI.system.history import HistoryManager
import os
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
            {
                "role": "tool",
                "content": '{"success": true}',
                "tool_call_id": "missing",
                "name": "read_file",
            }
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


def test_load_session_preserves_provider_compatible_tool_arguments(temp_history):
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
                        "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "content": '{"success": true}',
                "tool_call_id": "call_1",
                "name": "read_file",
            },
        ],
        "model": "claude",
    }
    with open(temp_history.history_dir / f"{sid}.json", "w") as f:
        json.dump(payload, f)

    session = temp_history.load_session(sid)
    assert session is not None
    args = session.messages[0].tool_calls[0]["function"]["arguments"]
    assert isinstance(args, str)
    assert json.loads(args) == {"path": "README.md"}

    temp_history.save_session(session)
    reloaded = temp_history.load_session(sid)
    assert reloaded is not None
    reloaded_args = reloaded.messages[0].tool_calls[0]["function"]["arguments"]
    assert isinstance(reloaded_args, str)
    assert json.loads(reloaded_args) == {"path": "README.md"}


def test_cleanup_expired_sessions_removes_full_session_id_from_index(temp_history):
    sid = f"session_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    session_file = temp_history.history_dir / f"{sid}.json"
    with open(session_file, "w") as f:
        json.dump({"session_id": sid, "updated_at": 1, "messages": [], "model": "claude"}, f)

    index_file = temp_history.history_dir / "index.json"
    with open(index_file, "w") as f:
        json.dump({sid: {"session_id": sid}}, f)

    old = time.time() - (31 * 24 * 60 * 60)
    os.utime(session_file, (old, old))

    temp_history._cleanup_expired_sessions()

    assert not session_file.exists()
    with open(index_file, "r") as f:
        assert sid not in json.load(f)
