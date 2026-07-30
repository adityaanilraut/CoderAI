"""Real Plan Mode persistence, routing, artifact, resume, and safety coverage."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.agent_session import AgentSessionMixin
from coderAI.core.planning import (
    PlanProposal,
    PlanQuestionSpec,
    PlanRiskSpec,
    PlanStepSpec,
    PlanStore,
    build_execution_prompt,
)
from coderAI.core.services import services_scope
from coderAI.core.tool_executor import ToolExecutor
from coderAI.system.config import Config
from coderAI.system.history import Session
from coderAI.tools.base import ToolRegistry
from coderAI.tools.filesystem import ReadFileTool, WriteFileTool
from coderAI.tools.planning import RequestPlanAmendmentTool, SubmitPlanTool


def _proposal(*, questions: list[str] | None = None) -> PlanProposal:
    return PlanProposal(
        summary="Implement parser validation without changing the public API.",
        success_criteria=["Invalid input is rejected", "Existing tests still pass"],
        in_scope=["Parser validation"],
        out_of_scope=["Parser rewrite"],
        assumptions=["Python 3.10 remains supported"],
        decisions=["Validate at the parser boundary"],
        steps=[
            PlanStepSpec(
                id="step-1",
                title="Add validation",
                description="Validate tokens before constructing the AST.",
                files=["coderAI/parser.py"],
                checks=["pytest -q tests/test_parser.py"],
            )
        ],
        risks=[PlanRiskSpec(risk="Behavior drift", mitigation="Keep compatibility tests")],
        tests=["Malformed and valid parser fixtures"],
        questions=[
            PlanQuestionSpec(
                id=f"decision-{index}",
                prompt=prompt,
                choices=["SQLite", "Postgres"] if "SQLite" in prompt else [],
            )
            for index, prompt in enumerate(questions or [], start=1)
        ],
    )


def test_plan_store_preserves_revisions_and_approved_snapshot(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        first = store.create("Fix parser", _proposal(), source_session_id="session-1")
        revised = store.revise(first, _proposal(), "Keep Python 3.10 support")
        approved = store.approve(revised)

        assert first.revision == 1
        assert revised.revision == 2
        assert (store.root / first.plan_id / "revision-1.json").is_file()
        assert (store.root / first.plan_id / "revision-2.json").is_file()
        assert approved.approved_revision == 2
        assert approved.approved_snapshot_hash
        assert store.load_active().status == "approved"
        assert "revision 2" in build_execution_prompt(approved, store.load_revision(approved, 2))


def test_plan_with_questions_cannot_be_approved(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        record = store.create(
            "Choose storage", _proposal(questions=["SQLite or Postgres?"]), source_session_id=None
        )

        assert record.status == "needs_input"
        with pytest.raises(ValueError, match="unanswered questions"):
            store.approve(record)


def test_approved_revision_is_rejected_if_snapshot_changes(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        approved = store.approve(store.create("Fix parser", _proposal(), source_session_id=None))
        revision = store.root / approved.plan_id / "revision-1.json"
        revision.write_text(revision.read_text(encoding="utf-8") + " ", encoding="utf-8")

        with pytest.raises(ValueError, match="changed after approval"):
            store.load_revision(approved, 1)


def test_editable_artifact_applies_as_immutable_revision(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        first = store.create("Fix parser", _proposal(), source_session_id=None)
        draft_path = store.draft_path(first)
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        draft["amendment"] = "Cover empty tokens"
        draft["proposal"]["success_criteria"].append("Empty tokens are rejected")
        draft_path.write_text(json.dumps(draft), encoding="utf-8")

        with pytest.raises(ValueError, match="unapplied edits"):
            store.approve(first)
        revised = store.apply_draft(first)

        assert revised.revision == 2
        assert revised.amendment == "Cover empty tokens"
        assert store.load_revision(revised, 1).success_criteria == _proposal().success_criteria
        assert store.load_revision(revised, 2).success_criteria[-1] == "Empty tokens are rejected"
        assert store.revision_history(revised)[1]["amendment"] == "Cover empty tokens"


def test_editable_artifact_rejects_stale_and_invalid_structure(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        first = store.create("Fix parser", _proposal(), source_session_id=None)
        draft_path = store.draft_path(first)
        stale = json.loads(draft_path.read_text(encoding="utf-8"))
        revised = store.revise(first, _proposal(), "No-op model revision")
        draft_path.write_text(json.dumps(stale), encoding="utf-8")
        with pytest.raises(ValueError, match="based on revision 1"):
            store.apply_draft(revised)

        store.refresh_draft(revised)
        invalid = json.loads(draft_path.read_text(encoding="utf-8"))
        invalid["proposal"]["steps"][0]["depends_on"] = ["missing"]
        draft_path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(ValueError, match="Unknown step dependencies"):
            store.apply_draft(revised)


def test_question_answers_are_stable_editable_revisions(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        first = store.create(
            "Choose storage", _proposal(questions=["SQLite or Postgres?"]), source_session_id=None
        )
        with pytest.raises(ValueError, match="one of"):
            store.answer_question(first, "decision-1", "Redis")

        answered = store.answer_question(first, "decision-1", "sqlite")
        approved = store.approve(answered)
        edited = store.answer_question(approved, "decision-1", "Postgres")

        assert answered.status == "draft"
        assert answered.proposal.questions[0].answer == "SQLite"
        assert edited.revision == 3
        assert edited.status == "draft"
        assert edited.approved_revision is None
        assert len(edited.approvals) == 1
        assert edited.approvals[0].revision == 2


def test_execution_attempt_and_amendment_history_survive_reload(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        approved = store.approve(store.create("Fix parser", _proposal(), source_session_id=None))
        executing = store.mark_executing(approved, execution_session_id="session-1")
        resumed = store.mark_executing(executing, execution_session_id="session-2", resume=True)
        replacement = _proposal().model_copy(deep=True)
        replacement.steps[0].description = "Validate before token normalization."
        amended = store.request_execution_amendment(resumed, replacement, "Boundary moved")
        loaded = store.load(amended.plan_id)

        assert loaded is not None
        assert loaded.status == "draft"
        assert loaded.approvals[0].revision == 1
        assert [attempt.status for attempt in loaded.executions] == [
            "interrupted",
            "amendment_requested",
        ]
        assert loaded.executions[1].session_id == "session-2"
        assert store.revision_history(loaded)[1]["amendment"].startswith(
            "Execution divergence from r1"
        )


@pytest.mark.asyncio
async def test_execution_amendment_tool_restores_read_only_boundary(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    agent = SimpleNamespace(
        plan_mode=False,
        tools=registry,
        tracker_info=None,
        config=Config(project_root=str(tmp_path)),
        auto_approve=True,
        active_plan_id=None,
        active_plan_revision=None,
        _cached_system_prompt=None,
        _refresh_session_system_prompt=lambda: None,
    )
    with services_scope(config=agent.config):
        store = PlanStore(str(tmp_path))
        approved = store.approve(store.create("Fix parser", _proposal(), source_session_id=None))
        executing = store.mark_executing(approved, execution_session_id="session-1")
        agent.active_plan_id = executing.plan_id
        agent.active_plan_revision = executing.approved_revision
        agent._plan_execution_ready = True
        amendment = RequestPlanAmendmentTool(agent)
        replacement = _proposal().model_copy(deep=True)
        replacement.steps[0].description = "Use a different boundary."

        result = await amendment.execute(reason="Parser API requires it", proposal=replacement)
        denied = await ToolExecutor(agent).execute_single_tool(
            {
                "tool_id": "write-after-amendment",
                "tool_name": "write_file",
                "arguments": {"path": "blocked.txt", "content": "no"},
                "parse_error": None,
            },
            hooks_data=None,
            hooks_manager=None,
        )

    assert result["success"] is True
    assert agent.plan_mode is True
    assert denied["success"] is False
    assert "no longer the executing revision" in denied["error"]
    assert not (tmp_path / "blocked.txt").exists()


def test_session_metadata_restores_and_clears_execution_linkage():
    class Holder(AgentSessionMixin):
        def __init__(self):
            self.session = Session(session_id="session_1_aaaaaaaa")
            self.active_plan_id = None
            self.active_plan_revision = None
            self._plan_execution_ready = False
            self.saved = 0

        def save_session(self):
            self.saved += 1

    first = Holder()
    first.set_active_plan_execution("a" * 32, 4)
    resumed = Holder()
    resumed.session.metadata = dict(first.session.metadata)
    resumed._restore_plan_execution(resumed.session)

    assert resumed.active_plan_id == "a" * 32
    assert resumed.active_plan_revision == 4
    assert resumed._plan_execution_ready is False
    resumed.clear_active_plan_execution()
    assert resumed.session.metadata == {}
    assert resumed.saved == 1


@pytest.mark.asyncio
async def test_submit_plan_rejects_unknown_dependencies():
    tool = SubmitPlanTool()
    proposal = _proposal().model_dump()
    proposal["steps"][0]["depends_on"] = ["missing-step"]

    result = await tool.execute(**proposal)

    assert result["success"] is False
    assert tool.last_proposal is None


@pytest.mark.asyncio
async def test_submit_plan_rejects_dependency_cycles():
    tool = SubmitPlanTool()
    proposal = _proposal().model_dump()
    proposal["steps"] = [
        {
            "id": "a",
            "title": "A",
            "description": "A",
            "depends_on": ["b"],
        },
        {
            "id": "b",
            "title": "B",
            "description": "B",
            "depends_on": ["a"],
        },
    ]

    result = await tool.execute(**proposal)

    assert result["success"] is False
    assert "cycle" in result["error"]


def test_plan_mode_schema_surface_is_read_only_plus_submit(mock_agent):
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(SubmitPlanTool())
    mock_agent.tools = registry
    mock_agent.plan_mode = True
    mock_agent.provider.supports_tools.return_value = True

    schemas = ExecutionLoop(mock_agent)._get_tool_schemas()
    names = {schema["function"]["name"] for schema in schemas or []}

    assert names == {"read_file", "submit_plan"}

    mock_agent.plan_mode = False
    normal_schemas = ExecutionLoop(mock_agent)._get_tool_schemas()
    normal_names = {schema["function"]["name"] for schema in normal_schemas or []}
    assert normal_names == {"read_file"}

    routed_schemas = ExecutionLoop(mock_agent)._get_tool_schemas("write the requested file")
    routed_names = {schema["function"]["name"] for schema in routed_schemas or []}
    assert routed_names == {"read_file", "write_file"}


@pytest.mark.asyncio
async def test_executor_denies_mutation_even_if_model_invents_it(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    agent = SimpleNamespace(
        plan_mode=True,
        tools=registry,
        tracker_info=None,
        config=Config(project_root=str(tmp_path)),
        auto_approve=True,
    )
    executor = ToolExecutor(agent)

    result = await executor.execute_single_tool(
        {
            "tool_id": "write-1",
            "tool_name": "write_file",
            "arguments": {"path": "should-not-exist.txt", "content": "no"},
            "parse_error": None,
        },
        hooks_data=None,
        hooks_manager=None,
    )

    assert result["success"] is False
    assert "blocked by enforced Plan Mode" in result["error"]
    assert not (tmp_path / "should-not-exist.txt").exists()


@pytest.mark.asyncio
async def test_plan_mode_suppresses_hooks_and_defers_mcp_launch(mock_agent):
    mock_agent.plan_mode = True
    mock_agent._mcp_initialized = False
    loop = ExecutionLoop(mock_agent)
    loop._ensure_workspace_trust = AsyncMock()
    loop._autoconnect_mcp_servers = AsyncMock()
    loop._call_llm_with_retry = AsyncMock(
        return_value={"content": "Read-only answer", "tool_calls": None, "finish_reason": "stop"}
    )

    result = await loop.run("Plan a safe change")

    assert result["success"] is True
    assert mock_agent.hooks_manager.load_hooks.call_count == 0
    loop._autoconnect_mcp_servers.assert_not_awaited()
    loop._ensure_workspace_trust.assert_not_awaited()
    assert mock_agent._mcp_initialized is False
