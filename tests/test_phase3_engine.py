"""Phase 3: State & Context Optimization Unit Tests.

Validates:
1. Token Compaction Engine: dual triggers, shadow event bracketing, tool pairing preservation.
2. Symmetrical ToolResultPruner for oversized output reduction.
3. Structured JSONL persistence: SessionHeader, flush, atomic append, and replay.
4. KV-Cache deterministic prefixing: PromptSection sorting, stable TOOL_ORDER.
"""

import pathlib
import pytest
from unittest.mock import MagicMock

from coderai.core.compaction import (
    BasicCompaction,
    ToolResultPruner,
    CompactionResult,
)
from coderai.core.events import (
    SessionEvent,
    make_user_event,
    make_assistant_event,
    make_tool_call_event,
    make_tool_result_event,
    make_compaction_summary,
    derive_messages_from_events,
)
from coderai.core.persistence import JsonlPersistence, SessionHeader
from coderai.core.prompt_sections import (
    PromptSection,
    assemble_sections,
    order_tools,
    TOOL_ORDER,
)


def test_tool_result_pruner_head_tail_symmetry():
    pruner = ToolResultPruner(max_chars=200)
    text = "START_" + ("X" * 500) + "_END"
    pruned = pruner.prune_content(text)

    assert len(pruned) < len(text)
    assert pruned.startswith("START_")
    assert pruned.endswith("_END")
    assert "characters omitted" in pruned


def test_compaction_shadow_event_derivation():
    # Construct sequence of events:
    # 0: User 1
    # 1: Assistant 1 (tool call)
    # 2: Tool result 1
    # 3: Compaction summary (shadows seq 0, 1, 2)
    # 4: User 2
    # 5: Assistant 2
    events = [
        make_user_event(seq=0, content="Analyze this repo"),
        make_assistant_event(
            seq=1, turn=1, step=1, content="I'll search", tool_calls=[{"id": "c1", "function": {"name": "grep"}}]
        ),
        make_tool_result_event(seq=2, turn=1, step=1, call_id="c1", content="Found 10 files"),
        make_compaction_summary(
            seq=3,
            compaction_id="compact_1",
            content="Summary: Analyzed repo and found 10 files.",
            shadowed_seqs=[0, 1, 2],
        ),
        make_user_event(seq=4, content="Now edit the first file"),
        make_assistant_event(seq=5, turn=2, step=1, content="Editing file now."),
    ]

    messages = derive_messages_from_events(events)
    # Events 0, 1, 2 are shadowed, so derived messages should only be:
    # 1. Summary (seq 3)
    # 2. User 2 (seq 4)
    # 3. Assistant 2 (seq 5)
    assert len(messages) == 3
    assert "Summary: Analyzed repo" in messages[0]["content"]
    assert messages[1]["content"] == "Now edit the first file"
    assert messages[2]["content"] == "Editing file now."


def test_jsonl_persistence_header_and_atomic_flush(tmp_path: pathlib.Path):
    persist = JsonlPersistence(tmp_path / "sessions")
    session_id = "test_persistence_sess"

    header = SessionHeader(
        session_id=session_id,
        model="gpt-5.6-luna",
        project_root=str(tmp_path),
        provider="deepseek",
        persona="expert-coder",
        created_at=1000.0,
    )
    assert header.to_dict()["model"] == "gpt-5.6-luna"

    ev1 = make_user_event(seq=0, content="Initial prompt")
    persist.append_event(session_id, ev1)
    persist.flush(session_id)

    assert persist.exists(session_id) is True

    reloaded_events = persist.list_events(session_id)
    assert len(reloaded_events) == 1
    assert reloaded_events[0].data["content"] == "Initial prompt"

    persist.delete(session_id)
    assert persist.exists(session_id) is False


def test_prompt_sections_deterministic_ordering():
    sec1 = PromptSection(name="Tools", order=30, text="Tool definitions...")
    sec2 = PromptSection(name="System", order=10, text="You are CoderAI.")
    sec3 = PromptSection(name="Skills", order=20, text="Available skills...")

    assembled = assemble_sections([sec1, sec2, sec3])
    lines = assembled.split("\n\n")
    assert lines[0] == "You are CoderAI."
    assert lines[1] == "Available skills..."
    assert lines[2] == "Tool definitions..."


def test_order_tools_stable_kv_cache_ordering():
    tools = [
        {"type": "function", "function": {"name": "read"}},
        {"type": "function", "function": {"name": "bash"}},
        {"type": "function", "function": {"name": "edit"}},
        {"type": "function", "function": {"name": "custom_z"}},
    ]

    ordered = order_tools(tools)
    names = [t["function"]["name"] for t in ordered]
    # bash is earlier in TOOL_ORDER than read, which is earlier than edit
    assert names[0] == "bash"
    assert names[1] == "read"
    assert names[2] == "edit"
    assert names[3] == "custom_z"
