"""Compaction conversion depends on an explicit callable interface."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coderai.soul.compaction import BasicCompaction
from coderai.soul.session.manager import SessionManager


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
