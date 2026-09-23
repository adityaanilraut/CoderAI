"""Runtime parity: token cap, context checkpoints, and D-Mail rewind."""

from __future__ import annotations

import pathlib

import pytest

from coderai.llm import apply_request_completion_cap, estimate_openai_request_tokens
from coderai.soul.coderaisoul import AgentLoop
from coderai.soul.denwarenji import DenwaRenjiError
from coderai.soul.session.manager import SessionManager, ToolDispatchResult
from coderai.tools.dmail import handle_send_dmail_tool
from coderai.tools.legacy.executor import _result_as_dict
from coderai.tools.legacy.types import ToolResult


def _manager(tmp_path: pathlib.Path) -> SessionManager:
    return SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {},
    )


def _open_session(mgr: SessionManager, session_id: str, prompt: str) -> None:
    """Register a session and its first user row without starting file history."""
    index = mgr._load_index()
    index["entries"].append(
        {
            "id": session_id,
            "summary": prompt[:100],
            "status": "ready",
            "activeTokens": 0,
            "createTime": "2026-01-01T00:00:00Z",
            "updateTime": "2026-01-01T00:00:00Z",
        }
    )
    mgr._save_index(index)
    mgr._append_message(mgr._build_message(session_id, "user", prompt))


def test_completion_cap_stays_inside_the_window():
    request = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "x" * 4000}],
    }
    cap = apply_request_completion_cap(
        request,
        context_limit=1500,
        active_tokens=0,
        response_budget=8000,
        safety_margin=1024,
    )
    assert request["max_tokens"] == cap
    assert "max_completion_tokens" not in request
    assert cap == 1

    newer = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]}
    newer_cap = apply_request_completion_cap(
        newer,
        context_limit=8000,
        active_tokens=10,
        response_budget=4000,
        safety_margin=0,
    )
    assert newer["max_completion_tokens"] == newer_cap
    assert "max_tokens" not in newer
    assert newer_cap == 4000


def test_pending_tool_payload_increases_the_estimate():
    short = [{"role": "user", "content": "hi"}]
    with_tool = [
        *short,
        {"role": "tool", "tool_call_id": "c1", "content": "y" * 8000},
    ]
    assert estimate_openai_request_tokens(with_tool) > estimate_openai_request_tokens(short)


def test_force_stop_is_visible_to_the_turn_loop():
    result = ToolResult(ok=True, name="read", output="same")
    result._force_stop_turn = True
    encoded = _result_as_dict(result)
    assert encoded["forceStopTurn"] is True
    dispatch = ToolDispatchResult(waiting=False, stop_reason="tool_call_repeat")
    assert not dispatch
    assert dispatch.stop_reason == "tool_call_repeat"
    assert ToolDispatchResult(waiting=True)


@pytest.mark.asyncio
async def test_checkpoint_revert_and_dmail(tmp_path: pathlib.Path):
    mgr = _manager(tmp_path)
    sid = "sess-dmail"
    _open_session(mgr, sid, "keep this prompt")

    with pytest.raises(DenwaRenjiError):
        mgr.stage_dmail(sid, "too early", 0)

    checkpoint_id = mgr.checkpoint_context(sid)
    mgr._append_message(mgr._build_message(sid, "assistant", "this should vanish"))
    assert mgr.context_checkpoint_count(sid) == 1
    assert mgr.get_soul(sid).checkpoint_count() == 1

    context = type("Ctx", (), {"session_id": sid, "manager": mgr})()
    staged = handle_send_dmail_tool(
        {"message": "undo the last step", "checkpoint_id": checkpoint_id},
        context,
    )
    assert staged.ok is True
    assert mgr.take_pending_dmail(sid) == ("undo the last step", checkpoint_id)
    mgr.stage_dmail(sid, "undo the last step", checkpoint_id)

    loop = AgentLoop(mgr, sid)
    assert await loop._rewind_dmail() is True
    contents = [m.content or "" for m in mgr.list_session_messages(sid)]
    assert not any("this should vanish" in text for text in contents)
    assert any("D-Mail content" in text and "undo the last step" in text for text in contents)
    assert all(m.role != "_checkpoint" for m in mgr.list_session_messages(sid))


@pytest.mark.asyncio
async def test_steer_is_appended_once(tmp_path: pathlib.Path):
    mgr = _manager(tmp_path)
    sid = "sess-steer"
    _open_session(mgr, sid, "start")
    mgr.steer_session(sid, "look at the tests")
    loop = AgentLoop(mgr, sid)
    assert loop._consume_steers() is True
    assert loop._consume_steers() is False
    assert any(m.content == "look at the tests" for m in mgr.list_session_messages(sid))
