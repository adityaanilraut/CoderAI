"""Unit and integration tests for Phase 1 upgrades.

Verifies:
1. Subagent runtime inbox steering (Gap 1.2)
2. Schedule reminder dispatch runtime (Gap 9.1)
3. Filesystem observation policy & anti-clobber gate (Gap 4.1)
4. Pre-compaction tool result payload pruner (Gap 6.2)
5. Universal tool output spill store with sha256 (Gap 2.3)
6. macOS seatbelt profile cleanup (Gap 5.1)
7. Dynamic skill hot-reloading (Gap 7.1)
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import pytest
from typing import Any

from coderai.core.agents import spawn_background_agent
from coderai.core.compaction import ToolResultPruner, prune_tool_results_for_compaction
from coderai.core.sandbox import (
    _SEATBELT_TEMP_FILES,
    delete_seatbelt_profile,
)
from coderai.core.schedule import ScheduleManager
from coderai.core.skill.registry import SkillRegistry
from coderai.core.spill import apply_spill_policy
from coderai.core.subagent import SubAgentManager, SubAgentSpec
from coderai.core.tools.observation import FileObservationTracker


# =========================================================================
# 1. Subagent Runtime Inbox Steering (Gap 1.2)
# =========================================================================


@pytest.mark.asyncio
async def test_subagent_runtime_inbox_steering(tmp_path: pathlib.Path) -> None:
    """Test that a running subagent checks handle.inbox and incorporates steering turns."""
    call_count = 0
    received_messages: list[list[dict[str, Any]]] = []

    def mock_client_factory():
        nonlocal call_count

        class MockChatCompletions:
            def create(self, **kwargs):
                nonlocal call_count
                call_count += 1
                msgs = kwargs.get("messages", [])
                received_messages.append(list(msgs))

                if call_count == 1:
                    # First turn: simulate model calling a tool or returning text
                    return {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Starting first step...",
                                    "tool_calls": [
                                        {
                                            "id": "tc1",
                                            "type": "function",
                                            "function": {
                                                "name": "read",
                                                "arguments": json_dumps(
                                                    {"file_path": str(tmp_path / "dummy.txt")}
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                    }
                else:
                    # Second turn: final response
                    return {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Task completed with steering applied.",
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 15, "completion_tokens": 15},
                    }

        class MockOpenAI:
            chat = type("Chat", (), {"completions": MockChatCompletions()})()

        return {
            "client": MockOpenAI(),
            "model": "gpt-5.6-luna",
            "thinkingEnabled": False,
        }

    # Create dummy file
    (tmp_path / "dummy.txt").write_text("hello", encoding="utf-8")

    manager = SubAgentManager(
        project_root=str(tmp_path),
        create_openai_client=mock_client_factory,
    )

    spec = SubAgentSpec(
        description="Test Steering",
        prompt="Initial prompt",
        mode="read_only",
        max_iterations=5,
    )

    # Spawn background agent
    handle = await spawn_background_agent(manager, spec)

    # Immediately push steering message to inbox
    handle.inbox.append("Please focus on optimization.")

    # Await background task completion
    if handle.task:
        await handle.task

    assert handle.status == "completed"
    assert call_count == 2
    # Verify that turn 2 included the steering message
    assert len(received_messages) >= 2
    second_turn_msgs = received_messages[1]
    steering_found = any(
        "[Steering from parent]: Please focus on optimization." in m.get("content", "")
        for m in second_turn_msgs
        if m.get("role") == "user"
    )
    assert steering_found, "Subagent loop should have consumed inbox message into user turn"


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj)


# =========================================================================
# 2. Schedule Reminder Dispatch Runtime (Gap 9.1)
# =========================================================================


def test_schedule_reminder_manager(tmp_path: pathlib.Path) -> None:
    """Test ScheduleManager creation, session scoping, and check_due."""
    storage_file = str(tmp_path / "schedules.json")
    mgr = ScheduleManager(storage_path=storage_file)

    # Create an immediate / overdue schedule
    rec = mgr.create(
        prompt="Review unit test output",
        after_seconds=1,
        session_id="session_123",
    )
    assert rec.session_id == "session_123"
    assert rec.prompt == "Review unit test output"

    # Immediately check due: should not be due yet (or due in 1s)
    # Wait 1.1s
    time.sleep(1.1)

    # Check due for session_123
    due = mgr.check_due(session_id="session_123")
    assert len(due) == 1
    assert due[0].id == rec.id
    assert due[0].state == "dispatched"

    # Check due again: already dispatched
    due2 = mgr.check_due(session_id="session_123")
    assert len(due2) == 0


# =========================================================================
# 3. Filesystem Observation Policy & Anti-Clobber Gate (Gap 4.1)
# =========================================================================


def test_filesystem_observation_policy(tmp_path: pathlib.Path) -> None:
    """Test FileObservationTracker unobserved rejection, stale rejection, and success."""
    tracker = FileObservationTracker()
    test_file = tmp_path / "app.py"
    test_file.write_text("print('v1')", encoding="utf-8")

    session_id = "test_session_obs"

    # 1. Mutation before observation -> FS_NOT_OBSERVED
    allowed, err = tracker.check_mutation_allowed(session_id, str(test_file))
    assert not allowed
    assert "FS_NOT_OBSERVED" in (err or "")

    # 2. Non-existent file -> allowed without prior observation
    new_file = tmp_path / "new_module.py"
    allowed_new, err_new = tracker.check_mutation_allowed(session_id, str(new_file))
    assert allowed_new
    assert err_new is None

    # 3. Observe file
    tracker.record_observation(session_id, str(test_file), content="print('v1')")

    # 4. Mutation after observation -> Allowed
    allowed_after, err_after = tracker.check_mutation_allowed(session_id, str(test_file))
    assert allowed_after
    assert err_after is None

    # 5. External modification on disk -> FS_STALE_VERSION
    time.sleep(0.05)
    test_file.write_text("print('v2_external')", encoding="utf-8")
    allowed_stale, err_stale = tracker.check_mutation_allowed(session_id, str(test_file))
    assert not allowed_stale
    assert "FS_STALE_VERSION" in (err_stale or "")

    # 6. Re-observe -> Allowed again
    tracker.record_observation(session_id, str(test_file), content="print('v2_external')")
    allowed_reobserved, _ = tracker.check_mutation_allowed(session_id, str(test_file))
    assert allowed_reobserved


# =========================================================================
# 4. Pre-Compaction Tool Result Payload Pruner (Gap 6.2)
# =========================================================================


def test_compaction_tool_result_pruner() -> None:
    """Test ToolResultPruner symmetrically truncating bulky tool results."""
    pruner = ToolResultPruner(max_chars=200)

    short_text = "Short tool result output"
    assert pruner.prune_content(short_text) == short_text

    long_text = "A" * 100 + "MIDDLE_TO_OMIT" * 20 + "Z" * 100
    pruned = pruner.prune_content(long_text)
    assert len(pruned) < len(long_text)
    assert "characters omitted" in pruned
    assert pruned.startswith("A" * 50)
    assert pruned.endswith("Z" * 50)

    messages = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Run tests"},
        {"role": "tool", "content": long_text},
        {"role": "assistant", "content": "Done"},
    ]
    pruned_msgs = prune_tool_results_for_compaction(messages, max_chars=200)
    assert len(pruned_msgs) == 4
    assert "characters omitted" in pruned_msgs[2]["content"]
    assert pruned_msgs[0]["content"] == "You are a coding assistant."


# =========================================================================
# 5. Universal Tool Output Spill Store (Gap 2.3)
# =========================================================================


def test_universal_spill_store(tmp_path: pathlib.Path) -> None:
    """Test spilling oversized tool output with sha256."""
    session_id = "test_spill_sess"
    huge_content = "X" * 40_000

    replaced, ref = apply_spill_policy(
        huge_content,
        session_id=session_id,
        tool_name="bash",
        max_inline_bytes=5_000,
        root=tmp_path,
    )

    assert ref is not None
    assert ref.bytes == 40_000
    assert ref.sha256 is not None
    assert len(ref.sha256) == 64
    assert os.path.exists(ref.locator)
    assert "Full formatted result stored at:" in replaced


# =========================================================================
# 6. macOS Seatbelt Profile Cleanup (Gap 5.1)
# =========================================================================


def test_seatbelt_profile_cleanup(tmp_path: pathlib.Path) -> None:
    """Test that seatbelt temp profile tracking and deletion works cleanly."""
    # Create fake seatbelt temp file
    handle = tempfile.NamedTemporaryFile("w", prefix="coderai_sb_", suffix=".sb", delete=False)
    handle.write("(version 1)")
    handle.close()

    _SEATBELT_TEMP_FILES.add(handle.name)
    assert os.path.exists(handle.name)

    delete_seatbelt_profile(handle.name)
    assert not os.path.exists(handle.name)
    assert handle.name not in _SEATBELT_TEMP_FILES


# =========================================================================
# 7. Dynamic Skill Hot-Reloading (Gap 7.1)
# =========================================================================


def test_skill_hot_reloading(tmp_path: pathlib.Path) -> None:
    """Test that creating a skill on disk is immediately discovered by SkillRegistry without restarting."""
    skill_dir = tmp_path / ".coderai" / "skills" / "my-dynamic-skill"
    skill_dir.mkdir(parents=True)

    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: my-dynamic-skill\ndescription: Dynamic testing skill\n---\n# My Skill\nInstructions here.",
        encoding="utf-8",
    )

    registry = SkillRegistry(project_root=str(tmp_path))
    skills = registry.list_skills()
    skill_names = [s["name"] for s in skills]

    assert "my-dynamic-skill" in skill_names

    loaded = registry.load_skill("my-dynamic-skill")
    assert loaded is not None
    assert loaded["name"] == "my-dynamic-skill"
    assert "Instructions here." in loaded["content"]
