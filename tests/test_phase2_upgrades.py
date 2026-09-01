"""Tests for Phase 2 Architectural Upgrades in CoderAI.

Covers:
1. Subagent depth limits and token budget enforcement.
2. Compaction message preservation directives (pinned/preserve metadata and IDs).
3. Session store event replay and automatic invariant repair.
4. Skill frontmatter deprecation and version parsing/rendering.
5. Tool execution sliding-window rate limiting.
"""

from __future__ import annotations

import pathlib
from typing import Any
import pytest

from coderai.core.common.invariants import verify_session_invariants
from coderai.core.compaction import BasicCompaction
from coderai.core.session import SessionManager, SessionMessage
from coderai.core.session_store import JsonlSessionStore
from coderai.core.skill.loader import (
    extract_skill_frontmatter,
    render_skill_document_block,
)
from coderai.core.subagent import SubAgentManager, SubAgentSpec
from coderai.core.tools.executor import SlidingWindowRateLimiter, ToolExecutor
from coderai.core.tools.schema import define_tool
from coderai.core.tools.types import (
    TOOL_ABORTED_BEFORE_DISPATCH,
    ToolExecutionContext,
    ToolResult,
)


# ==============================================================================
# 1. Subagent Nesting Depth Limits & Token Budget Caps
# ==============================================================================

@pytest.mark.asyncio
async def test_subagent_depth_limit(tmp_path: pathlib.Path):
    """Verify subagent spawning fails with recursion limit when depth > max_depth."""
    mgr = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {},
    )
    spec = SubAgentSpec(
        description="Nested child task",
        prompt="Do something deep",
        depth=4,
        max_depth=3,
    )
    res = await mgr.spawn_subagent(spec)
    assert res.status == "failed"
    assert "RecursionLimitError" in (res.error or "")
    assert "exceeds max_depth" in (res.error or "")


@pytest.mark.asyncio
async def test_subagent_token_budget_cap(tmp_path: pathlib.Path):
    """Verify subagent loop halts gracefully with budget_exceeded when token budget is reached."""
    mgr = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": object(),
            "model": "test-model",
        },
    )

    # Monkeypatch LLM call inside subagent to simulate token consumption
    import coderai.core.subagent as subagent_mod

    def mock_call_llm_sync(client: Any, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {
                        "content": "Running first step...",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read_test", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 600,
                "completion_tokens": 500,
                "total_tokens": 1100,
            },
        }

    original_call = subagent_mod._call_llm_sync
    subagent_mod._call_llm_sync = mock_call_llm_sync

    try:
        spec = SubAgentSpec(
            description="Budgeted task",
            prompt="Analyze codebase",
            token_budget=1000,  # Cap at 1000 tokens (mock returns 1100)
        )
        res = await mgr.spawn_subagent(spec)
        assert res.status == "budget_exceeded"
        assert res.total_tokens >= 1000
    finally:
        subagent_mod._call_llm_sync = original_call


# ==============================================================================
# 2. Session Compaction Preservation Directives
# ==============================================================================

@pytest.mark.asyncio
async def test_compaction_preservation_directives(tmp_path: pathlib.Path):
    """Verify preserved and pinned messages are excluded from shadowed IDs during compaction."""
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {
            "client": object(),
            "model": "test-model",
        },
        get_resolved_settings=lambda: {},
    )
    sid = await mgr.create_session("Compaction preservation test")
    mgr._update_entry(sid, lambda e: {**e, "status": "idle", "failReason": None})

    # Seed messages: system, user (pinned), assistant, tool, assistant, user
    messages: list[SessionMessage] = [
        SessionMessage(id="msg_0", session_id=sid, role="system", content="System base"),
        SessionMessage(
            id="msg_1",
            session_id=sid,
            role="user",
            content="Critical user instructions (never forget)",
            meta={"pinned": True, "preserve": True},
        ),
        SessionMessage(
            id="msg_2",
            session_id=sid,
            role="assistant",
            content="Let me read files",
            tool_calls=[{"id": "tc1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        ),
        SessionMessage(
            id="msg_3",
            session_id=sid,
            role="tool",
            content="file data",
            tool_call_id="tc1",
        ),
        SessionMessage(id="msg_4", session_id=sid, role="assistant", content="Analysis results"),
        SessionMessage(id="msg_5", session_id=sid, role="user", content="Next task"),
    ]

    # Save messages to manager store
    mgr.session_store.replace_rows(sid, [m.to_dict() for m in messages])

    # Mock _create_completion on manager to avoid external API calls
    async def mock_create_completion(client: Any, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": "## Primary Request and Intent\n- Goal"}}],
            "usage": {"total_tokens": 150},
        }

    mgr._create_completion = mock_create_completion  # type: ignore[assignment]

    compaction = BasicCompaction(manager=mgr)
    result = await compaction.compact_region(sid, start_idx=1, end_idx=5)
    assert result is not None

    # msg_1 was marked with preserve/pinned -> must NOT be in shadowed_ids
    assert "msg_1" not in result.shadowed_ids
    # msg_2, msg_3, msg_4 were standard messages -> must be in shadowed_ids
    assert "msg_2" in result.shadowed_ids
    assert "msg_3" in result.shadowed_ids
    assert "msg_4" in result.shadowed_ids


# ==============================================================================
# 3. Session Store Event Replay & Invariant Self-Healing
# ==============================================================================

def test_session_store_replay_and_invariant_repair(tmp_path: pathlib.Path):
    """Verify session store replays events and self-heals unfulfilled tool calls."""
    store = JsonlSessionStore(project_root=str(tmp_path))
    sid = "repair_test_session"

    # Create a corrupted history with an unfulfilled assistant tool call (dangling tc_dangling)
    corrupted_rows = [
        {"id": "m1", "session_id": sid, "role": "user", "content": "hello"},
        {
            "id": "m2",
            "session_id": sid,
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "tc_dangling", "type": "function", "function": {"name": "test_fn", "arguments": "{}"}}],
        },
        # Missing tool result for tc_dangling!
        {"id": "m3", "session_id": sid, "role": "user", "content": "next turn prompt"},
    ]
    store.replace_rows(sid, corrupted_rows)

    # Invariants should detect the violation initially
    violations_before = verify_session_invariants(store.read_rows(sid))
    assert len(violations_before) > 0

    # Execute self-healing
    detected = store.validate_and_repair_invariants(sid)
    assert len(detected) > 0

    # Re-check repaired rows
    repaired_rows = store.read_rows(sid)
    violations_after = verify_session_invariants(repaired_rows)
    assert len(violations_after) == 0

    # Confirm synthetic abort was appended
    repair_msg = next((r for r in repaired_rows if r.get("tool_call_id") == "tc_dangling"), None)
    assert repair_msg is not None
    assert TOOL_ABORTED_BEFORE_DISPATCH in repair_msg.get("content", "")

    # Replay events
    events = store.replay_events(sid)
    assert len(events) == len(repaired_rows)


# ==============================================================================
# 4. Skill Deprecation & Version Negotiation
# ==============================================================================

def test_skill_frontmatter_version_and_deprecation():
    """Verify skill frontmatter parses version, min_runtime_version, and deprecated fields."""
    skill_content = """---
name: legacy-sql-tool
version: 2.1.0
min_runtime_version: 1.5.0
deprecated: Use modern-sql-pipeline instead
description: Old SQL transformation skill
---
# Instructions
Do some SQL.
"""
    meta = extract_skill_frontmatter(skill_content)
    assert meta.get("name") == "legacy-sql-tool"
    assert meta.get("version") == "2.1.0"
    assert meta.get("min_runtime_version") == "1.5.0"
    assert meta.get("deprecated") == "Use modern-sql-pipeline instead"

    rendered = render_skill_document_block({
        "name": "legacy-sql-tool",
        "content": skill_content,
        "version": meta.get("version"),
        "deprecated": meta.get("deprecated"),
    })

    assert 'version="2.1.0"' in rendered
    assert 'deprecated="Use modern-sql-pipeline instead"' in rendered
    assert "This skill is deprecated: Use modern-sql-pipeline instead" in rendered


# ==============================================================================
# 5. Tool Sliding-Window Rate Limiting
# ==============================================================================

@pytest.mark.asyncio
async def test_sliding_window_rate_limiter():
    """Verify SlidingWindowRateLimiter enforces call quotas within time windows."""
    limiter = SlidingWindowRateLimiter()

    # Limit: 2 calls per 1.0 second
    allowed1, retry1 = limiter.acquire("tool_a", max_requests=2, window_seconds=1.0)
    assert allowed1 is True
    assert retry1 == 0.0

    allowed2, retry2 = limiter.acquire("tool_a", max_requests=2, window_seconds=1.0)
    assert allowed2 is True
    assert retry2 == 0.0

    # Third call within window should be rejected
    allowed3, retry3 = limiter.acquire("tool_a", max_requests=2, window_seconds=1.0)
    assert allowed3 is False
    assert retry3 > 0.0


@pytest.mark.asyncio
async def test_tool_executor_rate_limiting(tmp_path: pathlib.Path):
    """Verify ToolExecutor blocks calls when tool rate_limit is exceeded."""
    executor = ToolExecutor(project_root=str(tmp_path))

    call_count = 0

    async def h_limited(args: dict[str, Any], ctx: ToolExecutionContext) -> ToolResult:
        nonlocal call_count
        call_count += 1
        return ToolResult(ok=True, name="rate_limited_fn", output=f"success {call_count}")

    # Register tool with rate limit of 1 call per 5.0 seconds
    executor.registry.register(
        define_tool(
            name="rate_limited_fn",
            handler=h_limited,
            rate_limit=(1, 5.0),
        )
    )

    # Call 1: should succeed
    res1 = await executor.execute_tool_call(
        "test_session",
        {"id": "c1", "type": "function", "function": {"name": "rate_limited_fn", "arguments": "{}"}},
    )
    assert res1.ok is True
    assert "success 1" in (res1.output or "")

    # Call 2: immediately afterwards should be rate limited
    res2 = await executor.execute_tool_call(
        "test_session",
        {"id": "c2", "type": "function", "function": {"name": "rate_limited_fn", "arguments": "{}"}},
    )
    assert res2.ok is False
    assert "ToolRateLimitExceeded" in (res2.error or "")
    assert res2.metadata.get("rateLimited") is True
