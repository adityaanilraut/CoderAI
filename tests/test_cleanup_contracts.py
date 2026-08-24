"""Behavior contracts preserved by the deep-cleanup refactor."""

from __future__ import annotations

import json

import pytest

from coderai.core.agent_loop import AgentLoop
from coderai.core.events import ASSISTANT_MESSAGE, USER_MESSAGE
from coderai.core.permissions import (
    describe_tool_permission_request,
    evaluate_permission_scopes,
    permission_coverage_gaps,
)
from coderai.core.session import SessionManager
from coderai.core.session_store import JsonlSessionStore
from coderai.core.tools.registry import get_tool_registry


def test_agent_loop_owns_activation_entrypoint():
    assert hasattr(AgentLoop, "run")
    assert callable(AgentLoop.run)


def test_every_registered_tool_has_permission_coverage():
    assert permission_coverage_gaps() == set()


def test_mixed_legacy_and_event_jsonl_reads(tmp_path):
    store = JsonlSessionStore(str(tmp_path))
    session_id = "mixed_session"
    store.replace_rows(
        session_id,
        [
            {
                "id": "legacy-user",
                "role": "user",
                "content": "hello from legacy",
                "createTime": "2026-01-01T00:00:00+00:00",
            },
            {
                "type": "assistant/message",
                "seq": 1,
                "time": 1_700_000_000_000,
                "data": {
                    "id": "event-assistant",
                    "content": "hello from event",
                    "toolCalls": None,
                },
            },
        ],
    )

    manager = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None},
        get_resolved_settings=lambda: {},
    )
    messages = manager.list_session_messages(session_id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "hello from legacy"
    assert messages[1].content == "hello from event"

    events = manager.list_session_events(session_id)
    assert [event.type for event in events] == [USER_MESSAGE, ASSISTANT_MESSAGE]


@pytest.mark.asyncio
async def test_headless_and_interactive_share_settings_factory(tmp_path):
    from coderai.cli.session_factory import build_session_manager

    settings_path = tmp_path / ".coderai" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"model": "factory-model", "toolsPreset": "core"}),
        encoding="utf-8",
    )

    interactive = build_session_manager(str(tmp_path), non_interactive=False)
    headless = build_session_manager(str(tmp_path), non_interactive=True, preset="shell_edit")
    assert interactive.get_resolved_settings()["model"] == "factory-model"
    assert headless.get_resolved_settings()["toolsPreset"] == "shell_edit"
    assert headless.non_interactive is True


def test_canonical_tool_presets_match_registry():
    registry = get_tool_registry()
    core = {
        schema["function"]["name"]
        for schema in registry.to_openai_schemas(options={"preset": "core"})
    }
    shell_edit = {
        schema["function"]["name"]
        for schema in registry.to_openai_schemas(options={"preset": "shell_edit"})
    }
    assert "bash" in core and "edit" in core and "str_replace_editor" in core
    assert shell_edit == {"bash", "str_replace_editor"}


def test_unregistered_tool_keeps_empty_scopes(tmp_path):
    request = describe_tool_permission_request(
        session_id="s1",
        project_root=str(tmp_path),
        tool_call={
            "id": "c1",
            "function": {"name": "definitely_not_a_real_tool", "arguments": "{}"},
        },
    )
    assert request["scopes"] == []
    assert evaluate_permission_scopes([]) == "allow"
