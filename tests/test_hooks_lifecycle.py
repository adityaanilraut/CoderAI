import json
import pytest
from unittest.mock import MagicMock, AsyncMock
from coderAI.core.agent import Agent
from coderAI.core.agent_loop import DOOM_LOOP_THRESHOLD, ExecutionLoop
from coderAI.system.error_policy import BudgetExceededError


@pytest.fixture
def mock_hooks_file(tmp_path):
    hooks_dir = tmp_path / ".coderAI"
    hooks_dir.mkdir()
    hooks_file = hooks_dir / "hooks.json"

    # We'll use a shell script that appends to a log file so we can verify it ran
    log_file = tmp_path / "hooks.log"

    hooks_config = {
        "hooks": [
            {
                "type": "on_user_prompt",
                "tool": "*",
                "command": f"echo 'on_user_prompt' >> {log_file}",
            },
            {"type": "on_stop", "tool": "*", "command": f"echo 'on_stop' >> {log_file}"},
            {"type": "on_compact", "tool": "*", "command": f"echo 'on_compact' >> {log_file}"},
        ]
    }
    hooks_file.write_text(json.dumps(hooks_config))
    return log_file


@pytest.mark.asyncio
async def test_hooks_lifecycle(tmp_path, mock_hooks_file):
    # Setup agent in the tmp_path project
    agent = Agent(auto_approve=True)
    agent.config.project_root = str(tmp_path)
    agent.config.save_history = False  # don't clutter

    # Mock LLM to just return a simple response immediately
    agent.provider.chat = AsyncMock(
        return_value={
            "choices": [
                {"message": {"content": "Hello there", "tool_calls": None}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )

    # 1. Test on_user_prompt and on_stop
    loop = ExecutionLoop(agent)
    await loop.run("test prompt")

    log_content = mock_hooks_file.read_text()
    assert "on_user_prompt" in log_content
    assert "on_stop" in log_content

    # 2. Test on_compact
    # Setup session with many messages to force compaction
    agent.create_session()
    for i in range(10):
        agent.session.add_message("user", f"user message {i}")
        agent.session.add_message("assistant", f"assistant message {i}")

    # Force compaction by returning a very high token count
    agent.context_controller.estimate_tokens = MagicMock(return_value=1000000)

    # Mock a summarization response structure
    agent.provider.chat = AsyncMock(
        return_value={
            "choices": [
                {
                    "message": {
                        "content": "This is a summary",
                    }
                }
            ]
        }
    )

    success = await agent.compact_context()
    assert success is True

    log_content = mock_hooks_file.read_text()
    assert "on_compact" in log_content


# ── Phase 1.3: on_stop now fires on EVERY terminal exit ──────────────────────
#
# Before the shared ``_finalize_turn`` these three exit paths (length / doom /
# budget) silently skipped the on_stop hook — the behavioral drift the plan
# calls out. Each test forces exactly one of those exits and asserts the hook
# ran by checking the shared hooks log.


def _trusted_agent(tmp_path):
    agent = Agent(auto_approve=True)
    agent.config.project_root = str(tmp_path)
    agent.config.save_history = False
    return agent


@pytest.mark.asyncio
async def test_on_stop_fires_on_length_stop(tmp_path, mock_hooks_file):
    agent = _trusted_agent(tmp_path)
    loop = ExecutionLoop(agent)

    # A truncated first response (no tools used yet) finalizes via the length
    # exit without triggering the one-shot concise-retry.
    loop._call_llm_with_retry = AsyncMock(
        return_value={
            "content": "partial answer",
            "tool_calls": None,
            "finish_reason": "length",
            "reasoning_content": None,
        }
    )

    result = await loop.run("please answer")

    assert "cut off" in result["content"].lower()
    assert "on_stop" in mock_hooks_file.read_text()


@pytest.mark.asyncio
async def test_on_stop_fires_on_doom_loop_stop(tmp_path, mock_hooks_file):
    agent = _trusted_agent(tmp_path)
    loop = ExecutionLoop(agent)

    # Same tool + args repeated within one batch trips the in-batch doom guard.
    tool_calls = [
        {
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "x"}'},
        }
        for i in range(DOOM_LOOP_THRESHOLD)
    ]
    loop._call_llm_with_retry = AsyncMock(
        return_value={
            "content": None,
            "tool_calls": tool_calls,
            "finish_reason": "tool_calls",
            "reasoning_content": None,
        }
    )

    result = await loop.run("do the thing")

    assert "loop" in result["content"].lower()
    assert "on_stop" in mock_hooks_file.read_text()


@pytest.mark.asyncio
async def test_on_stop_fires_on_budget_stop(tmp_path, mock_hooks_file):
    agent = _trusted_agent(tmp_path)
    loop = ExecutionLoop(agent)

    # A budget breach surfaced from the LLM call is a hard terminal stop.
    loop._call_llm_with_retry = AsyncMock(
        side_effect=BudgetExceededError("budget exhausted")
    )

    result = await loop.run("keep spending")

    assert "blocked" in result["content"].lower()
    assert "on_stop" in mock_hooks_file.read_text()
