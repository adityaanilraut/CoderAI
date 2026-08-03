"""Independent objective-ledger persistence and isolation coverage."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.agent_session import AgentSessionMixin
from coderAI.core.execution_context import create_run_context
from coderAI.core.objective import ObjectiveState
from coderAI.core.objective_store import ObjectiveLedgerError, ObjectiveLedgerStore
from coderAI.system.history import Session


def _bound_store(tmp_path, session_id: str = "session_objective"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = ObjectiveLedgerStore(
        session_id=session_id,
        ledger_root=tmp_path / "objectives",
    )
    context = replace(
        create_run_context(workspace_root=str(workspace)),
        session_id=session_id,
        objective_store=store,
    )
    return context, store


def _tool(*, category: str, read_only: bool) -> SimpleNamespace:
    return SimpleNamespace(category=category, is_read_only=read_only)


def test_objective_state_survives_store_reopen_with_completion_clocks(tmp_path) -> None:
    context, store = _bound_store(tmp_path)
    state = ObjectiveState("Fix the parser", plan_id="plan-123", plan_revision=4)
    state.bind_persistence(lambda current: store.save(current, run_context=context))
    state.record_tool_result(
        "write_file",
        {"path": "parser.py"},
        {"success": True},
        _tool(category="filesystem", read_only=False),
    )
    state.record_tool_result(
        "read_file",
        {"path": "parser.py"},
        {"success": True, "content": "fixed"},
        _tool(category="filesystem", read_only=True),
    )
    state.record_tool_result(
        "run_tests",
        {"path": "tests/test_parser.py"},
        {"success": True, "message": "3 passed"},
        _tool(category="code_quality", read_only=False),
    )

    reopened = ObjectiveLedgerStore(
        session_id="session_objective",
        ledger_root=tmp_path / "objectives",
    )
    restored = reopened.load_latest(run_context=context)

    assert restored is not None
    assert restored.objective_id == state.objective_id
    assert restored.plan_id == "plan-123"
    assert restored.artifacts_changed == ["parser.py"]
    assert restored.checks_completed == ["tests"]
    assert restored.evaluate_completion().status == "verified"


def test_session_binding_restores_latest_objective_without_transcript_context(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session(session_id="session_1234567890_deadbeef")

    original = SimpleNamespace(
        run_context=create_run_context(workspace_root=str(workspace)),
        config=SimpleNamespace(project_root=str(workspace)),
        last_objective_state=None,
    )
    AgentSessionMixin._bind_session_run_context(original, session)
    state = ObjectiveState("Resume durable work")
    original.run_context.objective_store.save(state, run_context=original.run_context)

    resumed = SimpleNamespace(
        run_context=create_run_context(workspace_root=str(workspace)),
        config=SimpleNamespace(project_root=str(workspace)),
        last_objective_state=None,
    )
    AgentSessionMixin._bind_session_run_context(resumed, session)

    assert resumed.last_objective_state.objective_id == state.objective_id
    assert resumed.last_objective_state.objective == "Resume durable work"
    resumed.last_objective_state.mark_unverified(["process interrupted"])
    restored = resumed.run_context.objective_store.load_latest(run_context=resumed.run_context)
    assert restored is not None
    assert restored.completion_status == "unverified"


def test_transcript_rewrite_cannot_remove_independent_objective_record(tmp_path) -> None:
    context, store = _bound_store(tmp_path)
    state = ObjectiveState("Inspect the configuration")
    state.bind_persistence(lambda current: store.save(current, run_context=context))
    assert state.evaluate_completion().status == "reasoned"
    record_path = store.store_dir / f"{state.objective_id}.json"
    before = json.loads(record_path.read_text(encoding="utf-8"))

    # Transcript compaction/rewind writes only the separate history artifact.
    transcript = tmp_path / "session.json"
    transcript.write_text('{"messages":[{"role":"system","content":"summary"}]}')
    transcript.write_text('{"messages":[]}')

    after = json.loads(record_path.read_text(encoding="utf-8"))
    assert after == before
    assert store.load(state.objective_id, run_context=context).completion_status == "reasoned"


@pytest.mark.parametrize("unsafe_id", ["", ".", "..", "../escape", "a/b"])
def test_objective_store_rejects_unsafe_session_ids(tmp_path, unsafe_id: str) -> None:
    with pytest.raises(ValueError, match="path-safe"):
        ObjectiveLedgerStore(session_id=unsafe_id, ledger_root=tmp_path)


def test_objective_store_rejects_cross_session_and_cross_workspace_reads(tmp_path) -> None:
    context, store = _bound_store(tmp_path)
    state = ObjectiveState("Keep evidence isolated")
    store.save(state, run_context=context)

    other_session = replace(context, session_id="session_other")
    with pytest.raises(ObjectiveLedgerError, match="does not own"):
        store.load(state.objective_id, run_context=other_session)

    other_workspace = replace(context, workspace_id="different")
    with pytest.raises(ObjectiveLedgerError, match="another workspace"):
        store.load(state.objective_id, run_context=other_workspace)


def test_objective_store_rejects_tampered_state_identity(tmp_path) -> None:
    context, store = _bound_store(tmp_path)
    state = ObjectiveState("Keep record identity stable")
    store.save(state, run_context=context)
    path = store.store_dir / f"{state.objective_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["state"]["objective_id"] = "objective_different"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ObjectiveLedgerError, match="state identity"):
        store.load(state.objective_id, run_context=context)


@pytest.mark.asyncio
async def test_execution_loop_persists_initial_and_terminal_objective(mock_agent, tmp_path) -> None:
    context, store = _bound_store(tmp_path)
    mock_agent.run_context = context
    mock_agent.last_objective_state = None
    loop = ExecutionLoop(mock_agent)
    loop._call_llm_with_retry = AsyncMock(
        return_value={"content": "Configuration inspected.", "tool_calls": None}
    )

    result = await loop.run("Inspect the configuration")
    restored = store.load_latest(run_context=context)

    assert result["success"] is True
    assert restored is not None
    assert restored.objective == "Inspect the configuration"
    assert restored.completion_status == "reasoned"
    assert mock_agent.last_objective_state.objective_id == restored.objective_id
