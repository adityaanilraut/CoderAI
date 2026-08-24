"""Comprehensive tests for Phase 1 Capabilities:
1. Context-Seeded Subagent Forking (subagent_fork)
2. KV-Cache Preserving Summarizer in Compaction
3. Subprocess Environment Variable Scrubbing
4. Model-Facing Persistent Bash Mode
5. Deterministic LLM Replay Engine
"""

from __future__ import annotations

import os
import pathlib
import pytest
from typing import Any
from unittest.mock import MagicMock

from coderai.core.common.shell_utils import (
    build_shell_env,
    is_sensitive_env_var,
    scrub_subprocess_env,
)
from coderai.core.compaction import BasicCompaction, ToolResultPruner
from coderai.core.subagent import SubAgentManager, SubAgentSpec
from coderai.core.tools.agents import handle_subagent_fork_tool
from coderai.core.tools.bash import handle_bash_tool
from coderai.core.tools.types import ToolExecutionContext
from tests.test_support.llm_replay import (
    ReplayClient,
    create_replay_client_factory,
)


# =========================================================================
# 1. Context-Seeded Subagent Forking
# =========================================================================


@pytest.mark.asyncio
async def test_subagent_spec_initializes_with_seed_messages(tmp_path: pathlib.Path) -> None:
    """Verify that SubAgentSpec with seed_messages sets up child message history with parent context."""
    received_messages: list[dict[str, Any]] = []

    def mock_factory():
        class MockCompletions:
            def create(self, **kwargs):
                nonlocal received_messages
                received_messages = list(kwargs.get("messages", []))
                return {
                    "choices": [
                        {"message": {"role": "assistant", "content": "Fork task finished."}}
                    ],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                }

        class MockOpenAI:
            chat = type("Chat", (), {"completions": MockCompletions()})()

        return {"client": MockOpenAI(), "model": "gpt-5.6-luna", "thinkingEnabled": False}

    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=mock_factory,
    )

    seed = [
        {"role": "user", "content": "Initial user request in parent"},
        {"role": "assistant", "content": "Parent analysis and plan"},
        {"role": "user", "content": "Follow up question in parent"},
    ]

    spec = SubAgentSpec(
        description="Verify seeded history",
        prompt="Execute child subtask with inherited context",
        seed_messages=seed,
    )

    result = await manager.spawn_subagent(spec)
    assert result.status == "completed"
    assert "Fork task finished" in result.summary

    # Verify initial messages passed to LLM
    assert len(received_messages) >= 5
    assert received_messages[0]["role"] == "system"
    assert received_messages[1]["role"] == "user"
    assert received_messages[1]["content"] == "Initial user request in parent"
    assert received_messages[2]["role"] == "assistant"
    assert received_messages[2]["content"] == "Parent analysis and plan"
    assert received_messages[3]["role"] == "user"
    assert received_messages[3]["content"] == "Follow up question in parent"
    assert received_messages[-1]["role"] == "user"
    assert "Execute child subtask with inherited context" in received_messages[-1]["content"]


@pytest.mark.asyncio
async def test_handle_subagent_fork_tool_extracts_seed_messages(tmp_path: pathlib.Path) -> None:
    """Verify handle_subagent_fork_tool extracts parent session messages and attaches seed metadata."""
    parent_messages = [
        type("Msg", (), {"role": "user", "content": "Parent turn 1"})(),
        type("Msg", (), {"role": "assistant", "content": "Parent response 1"})(),
    ]

    factory = create_replay_client_factory(
        [{"content": "Fork subagent accomplished task with context."}]
    )

    context = ToolExecutionContext(
        session_id="parent_sess_999",
        project_root=str(tmp_path),
        create_openai_client=factory,
        list_session_messages=lambda sid: parent_messages,
    )

    args = {
        "description": "Fork test",
        "prompt": "Inspect repo using parent context",
    }

    result = await handle_subagent_fork_tool(args, context)
    assert result.ok is True
    assert result.name == "subagent_fork"
    assert result.metadata is not None
    assert result.metadata.get("seededMessagesCount") == 2
    assert "Fork subagent accomplished task" in result.output


# =========================================================================
# 2. KV-Cache Preserving Compaction
# =========================================================================


@pytest.mark.asyncio
async def test_kv_cache_preserving_compaction(tmp_path: pathlib.Path) -> None:
    """Verify BasicCompaction preserves the conversation prefix and appends the directive as trailing user turn."""
    received_compaction_messages: list[dict[str, Any]] = []

    mock_manager = MagicMock()
    mock_manager.get_active_model = MagicMock(return_value="gpt-5.6-luna")
    mock_manager._next_seq = MagicMock(return_value=10)
    mock_manager._append_event = MagicMock()

    class MockClient:
        pass

    mock_manager.create_openai_client = MagicMock(return_value={"client": MockClient()})

    async def mock_create_completion(client, payload, emit_stream=False):
        nonlocal received_compaction_messages
        received_compaction_messages = list(payload.get("messages", []))
        return {
            "choices": [{"message": {"content": "## Primary Request and Intent\n- Test intent"}}],
            "usage": {"total_tokens": 120},
        }

    mock_manager._create_completion = mock_create_completion

    # Simulate messages in session
    test_msgs = [
        type("Msg", (), {"id": "m1", "role": "user", "content": "Hello, write code."})(),
        type("Msg", (), {"id": "m2", "role": "assistant", "content": "Here is the code."})(),
        type("Msg", (), {"id": "m3", "role": "user", "content": "Now run tests."})(),
    ]
    mock_manager.list_session_messages = MagicMock(return_value=test_msgs)

    compaction = BasicCompaction(mock_manager, pruner=ToolResultPruner(max_chars=1000))
    res = await compaction.compact_region("sess_test", start_idx=0, end_idx=3, trigger="pressure")

    assert res is not None
    assert "Test intent" in res.summary

    # Check that messages preserved conversation prefix + trailing compaction directive
    assert len(received_compaction_messages) == 4
    assert received_compaction_messages[0] == {"role": "user", "content": "Hello, write code."}
    assert received_compaction_messages[1] == {"role": "assistant", "content": "Here is the code."}
    assert received_compaction_messages[2] == {"role": "user", "content": "Now run tests."}
    assert received_compaction_messages[3]["role"] == "user"
    assert "compaction engine" in received_compaction_messages[3]["content"]


# =========================================================================
# 3. Subprocess Environment Scrubbing
# =========================================================================


def test_sensitive_env_var_detection() -> None:
    """Verify is_sensitive_env_var flags dangerous API keys and tokens."""
    assert is_sensitive_env_var("OPENAI_API_KEY") is True
    assert is_sensitive_env_var("DEEPSEEK_API_KEY") is True
    assert is_sensitive_env_var("ANTHROPIC_API_KEY") is True
    assert is_sensitive_env_var("CODERAI_API_KEY") is True
    assert is_sensitive_env_var("AWS_SECRET_ACCESS_KEY") is True
    assert is_sensitive_env_var("MY_CUSTOM_SECRET_KEY") is True
    assert is_sensitive_env_var("GITHUB_TOKEN") is True
    assert is_sensitive_env_var("AUTH_TOKEN") is True

    # Safe variables
    assert is_sensitive_env_var("PATH") is False
    assert is_sensitive_env_var("HOME") is False
    assert is_sensitive_env_var("SHELL") is False
    assert is_sensitive_env_var("LANG") is False
    assert is_sensitive_env_var("USER") is False


def test_scrub_subprocess_env_removes_sensitive_vars() -> None:
    """Verify scrub_subprocess_env strips sensitive ambient variables unless preserved."""
    test_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/test",
        "OPENAI_API_KEY": "sk-secret-key-12345",
        "DEEPSEEK_API_KEY": "dsh-secret-key",
        "GITHUB_TOKEN": "ghp_supersecret",
        "CUSTOM_APP_ENV": "production",
    }

    cleaned = scrub_subprocess_env(test_env)
    assert "OPENAI_API_KEY" not in cleaned
    assert "DEEPSEEK_API_KEY" not in cleaned
    assert "GITHUB_TOKEN" not in cleaned
    assert cleaned["PATH"] == "/usr/bin:/bin"
    assert cleaned["HOME"] == "/Users/test"
    assert cleaned["CUSTOM_APP_ENV"] == "production"

    # Preserving explicit user-configured keys
    preserved = scrub_subprocess_env(test_env, preserve_keys={"OPENAI_API_KEY"})
    assert preserved["OPENAI_API_KEY"] == "sk-secret-key-12345"
    assert "DEEPSEEK_API_KEY" not in preserved


def test_build_shell_env_scrubs_ambient_environment() -> None:
    """Verify build_shell_env automatically scrubs ambient os.environ."""
    os.environ["CODERAI_TEST_SECRET_KEY"] = "super-secret"
    try:
        env = build_shell_env(shell_path="/bin/bash")
        assert "CODERAI_TEST_SECRET_KEY" not in env
        assert env.get("NO_COLOR") == "1"
        assert env.get("PAGER") == "cat"
    finally:
        os.environ.pop("CODERAI_TEST_SECRET_KEY", None)


# =========================================================================
# 4. Model-Facing Persistent Bash Mode
# =========================================================================


def test_persistent_bash_execution(tmp_path: pathlib.Path) -> None:
    """Verify bash persistent mode retains environment and directory state."""
    import sys

    if sys.platform == "win32":
        pytest.skip("Persistent PTY test is POSIX only")

    context = ToolExecutionContext(
        session_id=f"test_pty_{int(os.getpid())}",
        project_root=str(tmp_path),
    )

    # 1. Export a variable in persistent shell
    res1 = handle_bash_tool(
        {"command": "export PERSISTENT_VAR='coderai_persistent_success'", "persistent": True},
        context,
    )
    assert res1.ok is True
    assert res1.metadata is not None
    assert res1.metadata.get("persistent") is True

    # 2. Read exported variable in follow-up command in same session
    res2 = handle_bash_tool(
        {"command": "echo $PERSISTENT_VAR", "persistent": True},
        context,
    )
    assert res2.ok is True
    assert "coderai_persistent_success" in res2.output


# =========================================================================
# 5. Deterministic LLM Replay Engine
# =========================================================================


def test_deterministic_llm_replay_client() -> None:
    """Verify ReplayClient generates deterministic responses and tracks call history."""
    entries = [
        "First deterministic answer",
        {
            "content": "Second structured answer",
            "tool_calls": [{"id": "tc1", "function": {"name": "read"}}],
        },
    ]
    client = ReplayClient(entries)

    resp1 = client.chat.completions.create(
        model="replay-model", messages=[{"role": "user", "content": "Hi"}]
    )
    assert resp1.choices[0].message.content == "First deterministic answer"

    resp2 = client.chat.completions.create(
        model="replay-model", messages=[{"role": "user", "content": "Next"}]
    )
    assert resp2.choices[0].message.content == "Second structured answer"
    assert resp2.choices[0].message.tool_calls[0]["id"] == "tc1"

    assert len(client.call_history) == 2


def test_deterministic_llm_replay_streaming() -> None:
    """Verify ReplayClient streaming chunks."""
    entries = ["Hello world streaming replay"]
    client = ReplayClient(entries)

    stream = client.chat.completions.create(
        model="replay-model", messages=[{"role": "user", "content": "Hi"}], stream=True
    )
    chunks = list(stream)
    assert len(chunks) > 1
    reconstructed = "".join(c.choices[0].delta.content or "" for c in chunks)
    assert reconstructed == "Hello world streaming replay"
