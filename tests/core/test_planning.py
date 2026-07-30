"""Real Plan Mode persistence, routing, and safety coverage."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.planning import (
    PlanProposal,
    PlanRiskSpec,
    PlanStepSpec,
    PlanStore,
    build_execution_prompt,
)
from coderAI.core.services import services_scope
from coderAI.core.tool_executor import ToolExecutor
from coderAI.system.config import Config
from coderAI.tools.base import ToolRegistry
from coderAI.tools.filesystem import ReadFileTool, WriteFileTool
from coderAI.tools.planning import SubmitPlanTool


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
        unanswered_questions=questions or [],
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
    assert normal_names == {"read_file", "write_file"}


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
