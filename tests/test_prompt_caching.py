"""Tests for DeepSeek prompt caching, bitwise identical prefix verification, and usage extraction."""

from __future__ import annotations

import json
import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from coderai.cli.exit_summary import compute_session_stats
from coderai.cli.interactive_menu import estimate_model_cost
from coderai.core.common.message_converter import OpenAIMessageConverter
from coderai.core.common.usage import accumulate_usage_dict, extract_usage_dict
from coderai.core.prompt import (
    get_runtime_context,
    get_subagent_system_prompt,
    get_system_prompt,
    get_tools,
)
from coderai.core.session import SessionManager, SessionMessage
from coderai.core.subagent import SubAgentManager, SubAgentSpec


def test_extract_usage_dict_deepseek_format():
    """Verify DeepSeek format with prompt_cache_hit_tokens and prompt_cache_miss_tokens."""
    raw = {
        "prompt_tokens": 1200,
        "completion_tokens": 150,
        "total_tokens": 1350,
        "prompt_cache_hit_tokens": 1024,
        "prompt_cache_miss_tokens": 176,
    }
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 1200
    assert extracted["completion_tokens"] == 150
    assert extracted["total_tokens"] == 1350
    assert extracted["cached_tokens"] == 1024
    assert extracted["prompt_cache_hit_tokens"] == 1024
    assert extracted["prompt_cache_miss_tokens"] == 176


def test_extract_usage_dict_openai_format():
    """Verify OpenAI format with prompt_tokens_details.cached_tokens."""
    raw = {
        "prompt_tokens": 2000,
        "completion_tokens": 300,
        "total_tokens": 2300,
        "prompt_tokens_details": {"cached_tokens": 1500},
    }
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 2000
    assert extracted["cached_tokens"] == 1500
    assert extracted["prompt_cache_hit_tokens"] == 1500
    assert extracted["prompt_cache_miss_tokens"] == 500


def test_extract_usage_dict_object_attributes():
    """Verify object format (as returned by openai-python SDK)."""
    details = type("Details", (), {"cached_tokens": 640})()
    raw = type(
        "Usage",
        (),
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_cache_hit_tokens": 640,
            "prompt_cache_miss_tokens": 360,
            "prompt_tokens_details": details,
        },
    )()
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 1000
    assert extracted["cached_tokens"] == 640
    assert extracted["prompt_cache_hit_tokens"] == 640
    assert extracted["prompt_cache_miss_tokens"] == 360


def test_accumulate_usage_dict():
    """Verify usage accumulation preserves cached and miss token counts."""
    u1 = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 1000,
    }
    u2 = {
        "prompt_tokens": 1200,
        "completion_tokens": 150,
        "total_tokens": 1350,
        "prompt_cache_hit_tokens": 1000,
        "prompt_cache_miss_tokens": 200,
    }
    acc = accumulate_usage_dict(None, u1)
    assert acc["prompt_tokens"] == 1000
    assert acc["cached_tokens"] == 0
    assert acc["prompt_cache_hit_tokens"] == 0

    acc = accumulate_usage_dict(acc, u2)
    assert acc["prompt_tokens"] == 2200
    assert acc["completion_tokens"] == 250
    assert acc["total_tokens"] == 2450
    assert acc["cached_tokens"] == 1000
    assert acc["prompt_cache_hit_tokens"] == 1000
    assert acc["prompt_cache_miss_tokens"] == 1200


def test_subagent_system_prompt_determinism():
    """Verify subagent system prompt is identical across different subtask descriptions."""
    p1 = get_subagent_system_prompt("read_only")
    p2 = get_subagent_system_prompt("read_only")
    assert p1 == p2
    assert "READ-ONLY" in p1

    gen1 = get_subagent_system_prompt("general")
    gen2 = get_subagent_system_prompt("general")
    assert gen1 == gen2
    assert "GENERAL" in gen1


def test_runtime_context_determinism(tmp_path: pathlib.Path):
    """Verify get_runtime_context produces deterministic JSON output."""
    ctx1 = get_runtime_context(str(tmp_path), "deepseek-v4-pro")
    ctx2 = get_runtime_context(str(tmp_path), "deepseek-v4-pro")
    assert ctx1 == ctx2
    assert "deepseek-v4-pro" in ctx1
    assert "Local Workspace Environment" in ctx1


def test_prompt_prefix_bitwise_identity_across_agent_iterations(tmp_path: pathlib.Path):
    """Verify that converted request payloads across iterations have bitwise identical prefixes."""
    converter = OpenAIMessageConverter()

    # Iteration 1 messages
    sys_prompt = get_system_prompt({"workspaceRoot": str(tmp_path)})
    runtime_ctx = get_runtime_context(str(tmp_path), "deepseek-v4-pro")

    iter1_session_msgs = [
        SessionMessage(id="m1", session_id="s1", role="system", content=sys_prompt),
        SessionMessage(id="m2", session_id="s1", role="system", content=runtime_ctx),
        SessionMessage(id="m3", session_id="s1", role="user", content="Refactor this function"),
    ]

    iter1_payload = converter.convert_session_messages(
        iter1_session_msgs, "deepseek-v4-pro", thinking_enabled=True
    )

    # Iteration 2 messages (assistant tool call + tool response added)
    iter2_session_msgs = list(iter1_session_msgs) + [
        SessionMessage(
            id="m4",
            session_id="s1",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"file_path": "main.py"}'},
                }
            ],
            thinking="I need to inspect main.py first.",
        ),
        SessionMessage(
            id="m5",
            session_id="s1",
            role="tool",
            content='{"ok": true, "output": "def foo(): pass"}',
            tool_call_id="call_123",
        ),
    ]

    iter2_payload = converter.convert_session_messages(
        iter2_session_msgs, "deepseek-v4-pro", thinking_enabled=True
    )

    # Verify iteration 1 payload is a strict bitwise identical prefix of iteration 2 payload
    assert len(iter2_payload) > len(iter1_payload)
    for i in range(len(iter1_payload)):
        assert iter1_payload[i] == iter2_payload[i], f"Mismatch at message index {i}"

    # Iteration 3 messages (second assistant turn + tool response)
    iter3_session_msgs = list(iter2_session_msgs) + [
        SessionMessage(
            id="m6",
            session_id="s1",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_456",
                    "type": "function",
                    "function": {"name": "edit", "arguments": '{"snippet_id": "sn1"}'},
                }
            ],
            thinking="Now editing foo() to add docstring.",
        ),
        SessionMessage(
            id="m7",
            session_id="s1",
            role="tool",
            content='{"ok": true, "output": "edited"}',
            tool_call_id="call_456",
        ),
    ]

    iter3_payload = converter.convert_session_messages(
        iter3_session_msgs, "deepseek-v4-pro", thinking_enabled=True
    )

    assert len(iter3_payload) > len(iter2_payload)
    for i in range(len(iter2_payload)):
        assert iter2_payload[i] == iter3_payload[i], f"Mismatch at message index {i}"


@pytest.mark.asyncio
async def test_session_manager_tracks_cached_tokens(tmp_path: pathlib.Path):
    """Verify SessionManager accumulates cached_tokens from API responses and estimates cost correctly."""
    call_count = 0

    def mock_create(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Turn 1: Cache miss on initial prompt
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Step 1",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"file_path": "a.txt"}'},
                                }
                            ],
                            "reasoning_content": "Thinking 1",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 1000,
                },
            }
        else:
            # Turn 2: Cache hit on prefix (1000 tokens cached)
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Done with task.",
                            "tool_calls": None,
                            "reasoning_content": "Thinking 2",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 60,
                    "total_tokens": 1260,
                    "prompt_cache_hit_tokens": 1000,
                    "prompt_cache_miss_tokens": 200,
                },
            }

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": mock_client,
            "model": "deepseek-v4-pro",
            "thinkingEnabled": True,
            "reasoningEffort": "max",
        },
        get_resolved_settings=lambda: {
            "model": "deepseek-v4-pro",
            "permissions": {"defaultMode": "allowAll"},
        },
    )

    (tmp_path / "a.txt").write_text("hello world")

    sid = await mgr.create_session("Inspect a.txt", skills=[])
    entry = mgr.get_session(sid)
    assert entry is not None
    assert entry.usage is not None
    assert entry.usage["cached_tokens"] == 1000
    assert entry.usage["prompt_cache_hit_tokens"] == 1000
    assert entry.usage["prompt_tokens"] == 2200
    assert entry.usage["completion_tokens"] == 110
    assert entry.usage["uncached_tokens"] == 1200

    # Verify session stats
    stats = compute_session_stats(mgr, sid)
    assert stats["cached_tokens"] == 1000
    assert stats["prompt_tokens"] == 2200
    assert stats["estimated_cost"] > 0

    # Verify message JSONL records turn usage and timestamps
    jsonl_path = mgr._messages_path(sid)
    assert jsonl_path.exists()
    lines = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) > 0
    for line_obj in lines:
        assert "timestamp" in line_obj
        assert isinstance(line_obj["timestamp"], int)
        assert line_obj["timestamp"] > 0

    # Check assistant message has usage in meta
    assistant_msgs = [m for m in lines if m["role"] == "assistant"]
    assert len(assistant_msgs) >= 1
    assert assistant_msgs[0].get("meta", {}).get("usage") is not None
    assert "cached_tokens" in assistant_msgs[0]["meta"]["usage"]

    # Check tool message has execution timestamps in meta
    tool_msgs = [m for m in lines if m["role"] == "tool"]
    assert len(tool_msgs) >= 1
    assert "startTime" in tool_msgs[0]["meta"]
    assert "endTime" in tool_msgs[0]["meta"]
    assert "durationMs" in tool_msgs[0]["meta"]


def test_extract_usage_dict_anthropic_format():
    """Verify Anthropic Claude format with input_tokens, output_tokens, cache_read_input_tokens, and cache_creation_input_tokens."""
    raw = {
        "input_tokens": 5000,
        "output_tokens": 450,
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 1000,
    }
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 5000
    assert extracted["completion_tokens"] == 450
    assert extracted["total_tokens"] == 5450
    assert extracted["cached_tokens"] == 4000
    assert extracted["uncached_tokens"] == 1000
    assert extracted["prompt_cache_hit_tokens"] == 4000
    assert extracted["cache_read_input_tokens"] == 4000
    assert extracted["cache_creation_input_tokens"] == 1000


def test_extract_usage_dict_openrouter_format():
    """Verify OpenRouter format with prompt_tokens_details and cached_tokens."""
    raw = {
        "prompt_tokens": 3000,
        "completion_tokens": 200,
        "total_tokens": 3200,
        "prompt_tokens_details": {"cached_tokens": 2500},
    }
    extracted = extract_usage_dict(raw)
    assert extracted["prompt_tokens"] == 3000
    assert extracted["completion_tokens"] == 200
    assert extracted["cached_tokens"] == 2500
    assert extracted["uncached_tokens"] == 500
    assert extracted["prompt_cache_hit_tokens"] == 2500
    assert extracted["prompt_cache_miss_tokens"] == 500


@pytest.mark.asyncio
async def test_tool_executor_timestamps(tmp_path: pathlib.Path):
    """Verify ToolExecutor attaches millisecond startTime, endTime, and durationMs."""
    from coderai.core.tools.executor import ToolExecutor

    executor = ToolExecutor(project_root=str(tmp_path))
    (tmp_path / "sample.txt").write_text("hello timestamps")

    call = {
        "id": "c_time_1",
        "type": "function",
        "function": {"name": "read", "arguments": json.dumps({"file_path": "sample.txt"})},
    }
    res = await executor.execute_tool_call("s_test", call)
    assert res.ok is True
    assert res.metadata is not None
    assert "startTime" in res.metadata
    assert "endTime" in res.metadata
    assert "durationMs" in res.metadata
    assert res.metadata["durationMs"] >= 0
    assert res.metadata["endTime"] >= res.metadata["startTime"]


def test_prune_tool_results_multi_turn_history():
    """Verify historical tool outputs are pruned while recent tool outputs retain full budget."""
    from coderai.core.session_log import (
        MAX_TOOL_RESULT_CHARS,
        prune_tool_results,
    )

    huge_past = "A" * 40_000    # exceeds MAX_TOOL_RESULT_CHARS → must be truncated
    huge_recent = "B" * 20_000  # under MAX_TOOL_RESULT_CHARS → must stay intact

    messages = [
        SessionMessage(id="u1", session_id="s", role="user", content="Turn 1"),
        SessionMessage(id="a1", session_id="s", role="assistant", content="Step 1", tool_calls=[{"id": "c1"}]),
        SessionMessage(id="t1", session_id="s", role="tool", content=huge_past, tool_call_id="c1"),
        SessionMessage(id="u2", session_id="s", role="user", content="Turn 2"),
        SessionMessage(id="a2", session_id="s", role="assistant", content="Step 2", tool_calls=[{"id": "c2"}]),
        SessionMessage(id="t2", session_id="s", role="tool", content=huge_recent, tool_call_id="c2"),
    ]

    pruned = prune_tool_results(messages)

    # Historical tool t1 should be pruned with the same MAX_TOOL_RESULT_CHARS limit
    t1_pruned = next(m for m in pruned if m.id == "t1")
    assert len(t1_pruned.content) < len(huge_past)
    assert "omitted" in t1_pruned.content
    assert len(t1_pruned.content) <= MAX_TOOL_RESULT_CHARS + 100

    # Recent tool t2 is within MAX_TOOL_RESULT_CHARS (20k <= 32k) and should NOT be truncated
    t2_pruned = next(m for m in pruned if m.id == "t2")
    assert t2_pruned.content == huge_recent


@pytest.mark.asyncio
async def test_exec_runner_clean_exit_code_zero(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Verify run_exec_session terminates cleanly with exit code 0."""
    from coderai.cli.exec_runner import run_exec_session

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Done successfully.",
                    "tool_calls": None,
                }
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 80,
        },
    }

    monkeypatch.setattr("coderai.cli.exec_runner._core_client", lambda: {"client": mock_client, "model": "gpt-4o"})

    exit_code = await run_exec_session(
        "Complete task",
        project_root=str(tmp_path),
        model="gpt-4o",
        auto_approve=True,
    )
    assert exit_code == 0

