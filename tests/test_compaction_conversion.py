"""Compaction conversion depends on an explicit callable interface."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
import json
from pathlib import Path

import pytest

from coderai.soul.compaction import BasicCompaction
from coderai.soul.session.manager import SessionManager
from coderai.soul.session.models import SessionMessage
from coderai.soul.compaction import estimate_context_tokens, estimate_text_tokens


@pytest.mark.parametrize("converter", [None, SimpleNamespace(), "custom", "mock-named"])
async def test_compaction_converter_and_empty_summary_are_keyless(tmp_path, converter):
    requests = []
    converted = []

    class Converter:
        def convert_session_messages(self, messages, **kwargs):
            converted.append((messages, kwargs))
            return [{"role": "user", "content": "custom-prefix"}]

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object()},
        get_resolved_settings=lambda: {"model": "test", "thinkingEnabled": True},
    )
    session_id = await manager.create_empty_session()
    for role in ("user", "assistant", "user"):
        manager._append_message(manager._build_message(session_id, role, "history"))
    before = manager.session_store.read_rows(session_id)
    if converter == "custom":
        manager.message_converter = Converter()
    elif converter == "mock-named":
        manager.message_converter = type("MockNamedConverter", (Converter,), {})()
    else:
        manager.message_converter = converter

    async def complete(session, client, request):
        requests.append(request)
        return {"choices": [{"message": {"content": ""}}]}

    manager._create_completion_with_retry = complete
    try:
        assert await BasicCompaction(manager).compact_region(session_id, 0, 2) is None
        assert len(requests) == 1 and requests[0]["tool_choice"] == "none"
        assert "tools" not in requests[0]
        assert manager.session_store.read_rows(session_id) == before
        if converter in ("custom", "mock-named"):
            assert requests[0]["messages"][0]["content"] == "custom-prefix"
            assert converted[0][1] == {"model": "test", "thinking_enabled": True}
        else:
            assert requests[0]["messages"][0]["role"] == "system"
            assert requests[0]["messages"][1]["content"] == "history"
    finally:
        from coderai.soul.session.approval import unregister_session_manager

        manager.close_event_streams()
        unregister_session_manager(manager)


def _message(role, content="", **kwargs):
    return SessionMessage(
        id=kwargs.pop("id", role), session_id="test", role=role, content=content, **kwargs
    )


def test_context_measurement_includes_pending_tool_results():
    assistant = _message("assistant", "done", meta={"usage": {"total_tokens": 1000}})
    pending = _message("tool", "x" * 480, tool_call_id="call")
    assert estimate_context_tokens([assistant, pending]) == 1120


def test_compaction_invalidates_old_usage_measurements():
    summary = _message("user", "brief", id="summary", meta={"isSummary": True})
    messages = [summary, _message("user", "continue", id="latest")]
    assert estimate_context_tokens(messages, 100_000) == estimate_text_tokens(messages)


def test_pinned_old_assistant_usage_is_not_reused_after_compaction():
    summary = _message(
        "user",
        "brief",
        id="summary",
        meta={"isSummary": True},
        create_time="2026-10-03T02:00:00+00:00",
    )
    old = _message(
        "assistant",
        "pinned",
        meta={"pinned": True, "usage": {"total_tokens": 100_000}},
        create_time="2026-10-03T01:00:00+00:00",
    )
    messages = [summary, old, _message("user", "continue", id="latest")]
    assert estimate_context_tokens(messages, 100_000) == estimate_text_tokens(messages)


def test_pruning_frozen_messages_never_mutates_source_history():
    from dataclasses import dataclass
    from coderai.soul.compaction import ToolResultPruner

    @dataclass(frozen=True)
    class FrozenTool:
        role: str = "tool"
        content: str = "x" * 1000

    source = FrozenTool()
    projected = ToolResultPruner(100).prune_messages([source])[0]
    assert len(projected.content) <= 100
    assert len(source.content) == 1000


def test_context_counting_strategies_match_reference():
    messages = [
        _message("assistant", "done", meta={"usage": {"total_tokens": 1000}}),
        _message("tool", "x" * 480, tool_call_id="call"),
    ]
    assert estimate_context_tokens(messages, strategy="measured") == 1000
    assert estimate_context_tokens(messages, strategy="estimated") == estimate_text_tokens(messages)
    assert estimate_context_tokens(messages) == 1120


def test_multimodal_context_uses_media_allowance_without_counting_base64():
    message = _message(
        "user",
        "describe",
        meta={
            "contentParams": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 80_000}}
            ]
        },
    )
    assert estimate_context_tokens([message]) == 2002


def test_explicit_region_cannot_split_interleaved_tool_exchange():
    messages = [
        _message("user", id="u"),
        _message("assistant", tool_calls=[{"id": "a"}, {"id": "b"}]),
        _message("tool", id="ta", tool_call_id="a"),
        _message("user", "steer", id="steer"),
        _message("tool", id="tb", tool_call_id="b"),
        _message("user", "next", id="next"),
    ]
    assert BasicCompaction(SimpleNamespace())._find_safe_region(messages) == (0, 5)


@pytest.mark.usefixtures("isolated_home")
async def test_pinned_tool_result_preserves_entire_exchange(tmp_path):
    from coderai.soul.session.log import derive_messages
    from coderai.cli.session_factory import close_session_manager

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object()},
        get_resolved_settings=lambda: {"model": "test"},
    )
    sid = await manager.create_empty_session()
    messages = [
        manager._build_message(sid, "user", "goal"),
        manager._build_message(
            sid,
            "assistant",
            content="",
            tool_calls=[{"id": "call", "function": {"name": "read", "arguments": "{}"}}],
        ),
        manager._build_tool_message(sid, "call", "critical", tool_meta={"pinned": True}),
        manager._build_message(sid, "assistant", "other"),
        manager._build_message(sid, "user", "next"),
    ]
    for message in messages:
        manager._append_message(message)

    async def complete(*args):
        return {"choices": [{"message": {"content": "summary"}}], "usage": {"total_tokens": 99999}}

    manager._create_completion_with_retry = complete
    try:
        engine = BasicCompaction(manager)
        assert await engine.compact_region(sid, 1, 3) is None
        result = await engine.compact_region(sid, 1, 5)
        assert result is not None
        assert messages[1].id not in result.shadowed_ids
        assert messages[2].id not in result.shadowed_ids
        assert result.shadowed_token_count != 99999
        assert result.tokens_after == estimate_text_tokens(
            derive_messages(manager.list_session_messages(sid))
        )
        visible = derive_messages(manager.list_session_messages(sid))
        assert any(m.tool_call_id == "call" for m in visible)
    finally:
        await close_session_manager(manager)


@pytest.mark.usefixtures("isolated_home")
async def test_compaction_aborts_if_history_rewinds_during_summary(tmp_path):
    from coderai.cli.session_factory import close_session_manager

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object()},
        get_resolved_settings=lambda: {"model": "test"},
    )
    sid = await manager.create_empty_session()
    for role in ("user", "assistant", "user"):
        manager._append_message(manager._build_message(sid, role, "history"))
    rows = manager.session_store.read_rows(sid)

    async def complete(*args):
        manager.session_store.replace_rows(sid, rows[:1])
        manager._invalidate_messages_cache(sid)
        return {"choices": [{"message": {"content": "stale summary"}}]}

    manager._create_completion_with_retry = complete
    try:
        assert await BasicCompaction(manager).compact_region(sid, 0, 2) is None
        assert manager.session_store.read_rows(sid) == rows[:1]
    finally:
        await close_session_manager(manager)


@pytest.mark.usefixtures("isolated_home")
async def test_concurrent_compaction_keeps_focus_per_session(tmp_path):
    from coderai.cli.session_factory import close_session_manager

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object()},
        get_resolved_settings=lambda: {"model": "test"},
    )
    first = await manager.create_empty_session()
    second = await manager.create_empty_session()
    for sid in (first, second):
        for role in ("user", "assistant", "user"):
            manager._append_message(manager._build_message(sid, role, "history"))
    requests = {}
    ready = asyncio.Event()

    async def complete(sid, client, request):
        requests[sid] = request
        if len(requests) == 2:
            ready.set()
        await ready.wait()
        return {"choices": [{"message": {"content": "summary"}}]}

    manager._create_completion_with_retry = complete
    try:
        engine = BasicCompaction(manager)
        await asyncio.wait_for(
            asyncio.gather(
                engine.compact_now(first, custom_instruction="first focus"),
                engine.compact_now(second, custom_instruction="second focus"),
            ),
            3,
        )
        assert "first focus" in requests[first]["messages"][-1]["content"]
        assert "second focus" not in requests[first]["messages"][-1]["content"]
        assert "second focus" in requests[second]["messages"][-1]["content"]
    finally:
        await close_session_manager(manager)


@pytest.mark.usefixtures("isolated_home")
async def test_tool_output_spill_preserves_full_json_and_actionable_pointer(tmp_path):
    from coderai.cli.session_factory import close_session_manager

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {},
    )
    raw = json.dumps(
        {"ok": False, "output": "begin" + "x" * 80_000 + "end", "metadata": {"exitCode": 2}}
    )
    try:
        message = manager._build_tool_message("../untrusted-session", "call", raw)
        payload = json.loads(message.content)
        output_path = Path(payload["output_path"])
        assert output_path.is_relative_to(manager.session_store.project_dir / "tool-results")
        assert output_path.read_text() == raw
        assert len(message.content) <= 32_000
        assert payload["metadata"]["exitCode"] == 2
        assert payload["ok"] is False
        assert message.meta["output_path"] == str(output_path)
        assert output_path.stat().st_mode & 0o777 == 0o600
    finally:
        await close_session_manager(manager)


def test_event_projection_prunes_json_without_corrupting_envelope():
    from coderai.events import derive_messages_from_events, make_tool_result_event

    event = make_tool_result_event(
        0,
        1,
        1,
        "call",
        json.dumps({"ok": False, "output": "x" * 80_000, "metadata": {"exitCode": 2}}),
    )
    payload = json.loads(derive_messages_from_events([event])[0]["content"])
    assert payload["metadata"]["exitCode"] == 2
    assert payload["ok"] is False


def test_reserve_larger_than_window_does_not_force_compaction():
    from coderai.soul.compaction import evaluate_compaction_trigger, should_auto_compact

    assert evaluate_compaction_trigger(10, 32_000, reserved_context_size=50_000) is None
    assert not should_auto_compact(10, 32_000)
    assert not should_auto_compact(10, 0)


@pytest.mark.usefixtures("isolated_home")
async def test_compaction_of_typed_tool_events_has_stable_ids_and_shadow_sequences(tmp_path):
    from coderai.cli.session_factory import close_session_manager
    from coderai.events import make_user_event, make_assistant_event, make_tool_result_event
    from coderai.soul.session.log import derive_messages

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object()},
        get_resolved_settings=lambda: {"model": "test"},
    )
    sid = "typed-tools"
    manager._append_event(sid, make_user_event(0, "goal", message_id="u"))
    manager._append_event(
        sid,
        make_assistant_event(
            1,
            1,
            1,
            content="",
            tool_calls=[{"id": "call", "function": {"name": "read", "arguments": "{}"}}],
            message_id="a",
        ),
    )
    manager._append_event(sid, make_tool_result_event(2, 1, 1, "call", "data"))
    manager._append_event(sid, make_user_event(3, "next", message_id="next"))

    async def complete(*args):
        return {"choices": [{"message": {"content": "summary"}}]}

    manager._create_completion_with_retry = complete
    try:
        result = await BasicCompaction(manager).compact_region(sid, 0, 3)
        assert result is not None
        assert result.shadowed_seqs == [0, 1, 2]
        assert all(m.role != "tool" for m in derive_messages(manager.list_session_messages(sid)))
        rows = manager.session_store.read_rows(sid)
        manager._invalidate_messages_cache(sid)
        first_ids = [m.id for m in manager.list_session_messages(sid)]
        manager._invalidate_messages_cache(sid)
        assert first_ids == [m.id for m in manager.list_session_messages(sid)]
        assert manager.session_store.read_rows(sid) == rows
    finally:
        await close_session_manager(manager)


@pytest.mark.usefixtures("isolated_home")
async def test_truncated_summary_never_shadows_history(tmp_path):
    from coderai.cli.session_factory import close_session_manager

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object()},
        get_resolved_settings=lambda: {"model": "test"},
    )
    sid = await manager.create_empty_session()
    for role in ("user", "assistant", "user"):
        manager._append_message(manager._build_message(sid, role, "history"))
    rows = manager.session_store.read_rows(sid)

    async def complete(*args):
        return {
            "choices": [
                {"message": {"content": "incomplete checkpoint"}, "finish_reason": "length"}
            ]
        }

    manager._create_completion_with_retry = complete
    try:
        assert await BasicCompaction(manager).compact_region(sid, 1, 3) is None
        assert manager.session_store.read_rows(sid) == rows
    finally:
        await close_session_manager(manager)
