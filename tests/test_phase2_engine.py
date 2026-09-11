"""Unit tests for Phase 2: Engine Core (kosong + kaos)."""

from __future__ import annotations

from pathlib import Path

import pytest
from kaos.path import KaosPath
from kosong.message import Message, TextPart
from pydantic import SecretStr

from coderai.config import LLMModel, LLMProvider
from coderai.llm import (
    LLM,
    compute_max_completion_tokens,
    create_llm,
    derive_model_capabilities,
    estimate_request_tokens,
)
from coderai.session import Session
from coderai.soul.tool_context import get_session_id, set_session_id
from coderai.ui.shell.slash import completion_entries
from coderai.utils.sensitive import is_sensitive_file, sensitive_file_warning
from coderai.utils.slashcmd import SlashCommandRegistry, parse_slash_command_call


def test_llm_create_and_capabilities():
    provider = LLMProvider(type="_echo", base_url="http://none", api_key=SecretStr("dummy"))
    model = LLMModel(provider="test", model="claude-3-5-sonnet", max_context_size=200_000)
    llm = create_llm(provider, model)
    assert llm is not None
    assert isinstance(llm, LLM)
    assert llm.max_context_size == 200_000

    # Test capabilities
    caps_str = derive_model_capabilities("kimi-for-coding")
    assert "thinking" in caps_str
    assert "image_in" in caps_str

    caps_model = derive_model_capabilities(model)
    assert isinstance(caps_model, set)


def test_token_computation_and_estimation():
    cap = compute_max_completion_tokens(
        max_context_size=128000,
        input_tokens=10000,
        response_budget=4000,
    )
    assert cap == 4000

    cap_remaining = compute_max_completion_tokens(
        max_context_size=1000,
        input_tokens=900,
        response_budget=4000,
    )
    assert cap_remaining == 100

    msg = Message(role="user", content=[TextPart(text="Hello world! This is a test.")])
    est = estimate_request_tokens("System prompt here.", [], [msg])
    assert est > 0


def test_sensitive_file_detection():
    assert is_sensitive_file(".env") is True
    assert is_sensitive_file("/project/.env.local") is True
    assert is_sensitive_file(".env.example") is False
    assert is_sensitive_file("id_rsa") is True
    assert is_sensitive_file("src/main.py") is False

    warning = sensitive_file_warning([".env", "id_rsa"])
    assert "Skipped 2 sensitive file(s)" in warning


def test_slashcmd_registry_and_parser():
    reg = SlashCommandRegistry()

    @reg.command(name="ping", aliases=["p"])
    def ping_cmd(app, args):
        """Ping command."""
        return "pong"

    assert reg.find_command("ping") is not None
    assert reg.find_command("p") is not None
    assert reg.find_command("p").name == "ping"

    call = parse_slash_command_call("/ping now")
    assert call is not None
    assert call.name == "ping"
    assert call.args == "now"

    assert parse_slash_command_call("not a slash command") is None


def test_slash_catalog_commands():
    """The ACP-advertised catalog comes from the live Stack A slash catalog."""
    names = {name for name, _ in completion_entries()}
    assert {"/init", "/clear", "/compact", "/yolo", "/plan"} <= names


def test_tool_context_session_id():
    set_session_id("test-session-123")
    assert get_session_id() == "test-session-123"


@pytest.mark.asyncio
async def test_session_facade_lifecycle(tmp_path: Path):
    work_dir = KaosPath(str(tmp_path))
    session = await Session.create(work_dir)

    assert session.id is not None
    assert session.dir.exists()
    assert session.subagents_dir.exists()

    found = await Session.find(work_dir, session.id)
    assert found is not None
    assert found.id == session.id

    sessions = await Session.list(work_dir)
    # New empty session without content may be filtered by is_empty() or present
    assert isinstance(sessions, list)
