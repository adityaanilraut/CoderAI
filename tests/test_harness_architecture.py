"""Focused tests for runtime event, storage, skill, and state behavior.

Tests:
1. Event model: typed SessionEvent creation, serialization, derive_messages_from_events.
2. Compaction: ToolResultPruner, safe region discovery, CompactionResult.
3. Storage: mixed legacy/event JSONL replay and deletion.
4. Skill subsystem: SkillRegistry layered scanning, frontmatter parsing, match_skills.
5. State subsystem: SessionStateManager isolation, file version tracking, snippet scoping.
"""

import pathlib

from coderai.core.events import (
    SessionEvent,
    make_turn_start,
    make_turn_end,
    make_step_start,
    make_step_end,
    make_user_event,
    make_assistant_event,
    make_tool_result_event,
    make_compaction_summary,
    derive_messages_from_events,
)
from coderai.core.compaction import ToolResultPruner
from coderai.core.session_store import JsonlSessionStore
from coderai.core.skill import (
    SkillRegistry,
)
from coderai.core.state import SessionStateManager, FileState


def test_event_taxonomy_and_serialization():
    ev = make_turn_start(seq=0, turn=1)
    assert ev.seq == 0
    assert ev.type == "turn/start"
    assert ev.is_log_only is True
    assert ev.is_surface is False

    d = ev.to_dict()
    assert d["seq"] == 0
    assert d["type"] == "turn/start"
    assert "timestamp" in d

    ev_back = SessionEvent.from_dict(d)
    assert ev_back.seq == 0
    assert ev_back.type == "turn/start"


def test_derive_messages_from_events():
    events = [
        make_turn_start(seq=0, turn=1),
        make_step_start(seq=1, turn=1, step=1),
        make_user_event(seq=2, content="Hello"),
        make_assistant_event(
            seq=3,
            turn=1,
            step=1,
            content="I'll run a tool",
            tool_calls=[{"id": "c1", "function": {"name": "test"}}],
        ),
        make_tool_result_event(seq=4, turn=1, step=1, call_id="c1", content="Tool output ok"),
        make_step_end(seq=5, turn=1, step=1),
        make_turn_end(seq=6, turn=1, reason="natural"),
    ]

    msgs = derive_messages_from_events(events)
    assert len(msgs) == 3
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "I'll run a tool"
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["content"] == "Tool output ok"


def test_compaction_shadow_hides_events_in_derive():
    events = [
        make_user_event(seq=0, content="Old message 1"),
        make_assistant_event(seq=1, turn=1, step=1, content="Old reply 1"),
        make_compaction_summary(
            seq=2, compaction_id="c1", content="Summary of old messages", shadowed_seqs=[0, 1]
        ),
        make_user_event(seq=3, content="New message 2"),
    ]

    msgs = derive_messages_from_events(events)
    # Events 0 and 1 are shadowed, so we should see: summary (seq 2) + new message (seq 3)
    assert len(msgs) == 2
    assert "Summary of old messages" in msgs[0]["content"]
    assert msgs[1]["content"] == "New message 2"


def test_tool_result_pruner_symmetric_truncation():
    pruner = ToolResultPruner(max_chars=100)
    short_text = "Short output"
    assert pruner.prune_content(short_text) == short_text

    long_text = "A" * 500
    pruned = pruner.prune_content(long_text)
    assert len(pruned) < 500
    assert "characters omitted" in pruned
    assert pruned.startswith("A" * 50)
    assert pruned.endswith("A" * 50)


def test_jsonl_session_store_reads_mixed_rows(tmp_path: pathlib.Path):
    store = JsonlSessionStore(str(tmp_path))
    sid = "test_sess_001"

    ev1 = make_user_event(seq=0, content="First prompt")
    ev2 = make_assistant_event(seq=1, turn=1, step=1, content="Hello response")

    store.append_row(
        sid,
        {"id": "legacy", "sessionId": sid, "role": "user", "content": "Legacy prompt"},
    )
    store.append_row(sid, ev1.to_dict())
    store.append_row(sid, ev2.to_dict())

    reloaded = store.list_events(sid)
    assert len(reloaded) == 3
    assert reloaded[0].data["content"] == "Legacy prompt"
    assert reloaded[1].data["content"] == "First prompt"
    assert reloaded[2].data["content"] == "Hello response"
    assert store.delete_log(sid) is True


def test_skill_registry_and_frontmatter_parsing(tmp_path: pathlib.Path):
    skill_dir = tmp_path / ".coderai" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: test-skill\ndescription: A specialized testing skill\nallow-implicit-invocation: true\n---\n\nSkill body instructions here.\n",
        encoding="utf-8",
    )

    reg = SkillRegistry(project_root=str(tmp_path))
    skills = reg.list_skills()
    assert any(s["name"] == "test-skill" for s in skills)

    loaded = reg.load_skill("test-skill")
    assert loaded is not None
    assert "Skill body instructions here" in loaded["content"]

    matched = reg.match_skills("Please run test-skill on this code")
    assert any(s["name"] == "test-skill" for s in matched)


def test_session_state_manager_encapsulation():
    mgr = SessionStateManager()
    sid = "sess_iso_1"

    fstate = FileState(file_path="src/main.py", content="print('hello')", timestamp=1000)
    mgr.record_file_state(sid, fstate)

    assert mgr.was_file_read(sid, "src/main.py") is True
    assert mgr.get_file_version(sid, "src/main.py") == 0

    snip = mgr.create_snippet(sid, "src/main.py", 1, 1, "print('hello')")
    assert snip is not None
    assert snip.id == "snippet_1"
    assert mgr.get_snippet(sid, "snippet_1") == snip

    # Increment file version
    mgr.record_file_state(sid, fstate, increment_version=True)
    assert mgr.get_file_version(sid, "src/main.py") == 1
    assert mgr.has_snippet_outdated_file_version(sid, snip) is True

    # Clear state
    mgr.clear(sid)
    assert mgr.has_session_state(sid) is False
