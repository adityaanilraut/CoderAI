"""Security regressions for editable and executing Plan Mode boundaries."""

import json
from types import SimpleNamespace

import pytest

from coderAI.core.planning import PlanProposal, PlanStepSpec, PlanStore
from coderAI.core.services import services_scope
from coderAI.core.tool_executor import ToolExecutor
from coderAI.system.config import Config
from coderAI.tools.base import ToolRegistry
from coderAI.tools.filesystem import WriteFileTool


pytestmark = pytest.mark.security


def _proposal(description="Implement safely"):
    return PlanProposal(
        summary="Safe implementation",
        success_criteria=["Tests pass"],
        steps=[PlanStepSpec(id="step-1", title="Implement", description=description)],
    )


def test_plan_draft_import_cannot_escape_project(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    outside = tmp_path.parent / "outside-plan-draft.json"
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        record = store.create("Safe change", _proposal(), source_session_id=None)
        draft = json.loads(store.draft_path(record).read_text(encoding="utf-8"))
        draft["proposal"]["summary"] = "Escaped draft"
        outside.write_text(json.dumps(draft), encoding="utf-8")
        try:
            with pytest.raises(ValueError, match="inside project root"):
                store.apply_draft(record, str(outside))
        finally:
            outside.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_external_plan_amendment_blocks_next_execution_mutation(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    agent = SimpleNamespace(
        plan_mode=False,
        tools=registry,
        tracker_info=None,
        config=cfg,
        auto_approve=True,
        active_plan_id=None,
        active_plan_revision=None,
    )
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        approved = store.approve(store.create("Safe change", _proposal(), source_session_id=None))
        executing = store.mark_executing(approved, execution_session_id="session-1")
        agent.active_plan_id = executing.plan_id
        agent.active_plan_revision = executing.approved_revision
        agent._plan_execution_ready = True
        store.request_execution_amendment(
            executing,
            _proposal("Use a different implementation"),
            "The approved interface is unavailable",
        )

        denied = await ToolExecutor(agent).execute_single_tool(
            {
                "tool_id": "write-after-external-amendment",
                "tool_name": "write_file",
                "arguments": {"path": "blocked.txt", "content": "no"},
                "parse_error": None,
            },
            hooks_data=None,
            hooks_manager=None,
        )

    assert denied["success"] is False
    assert "no longer the executing revision" in denied["error"]
    assert not (tmp_path / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_restored_execution_requires_explicit_resume_before_mutation(tmp_path):
    cfg = Config(project_root=str(tmp_path))
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        approved = store.approve(store.create("Safe change", _proposal(), source_session_id=None))
        executing = store.mark_executing(approved, execution_session_id="session-1")
        agent = SimpleNamespace(
            plan_mode=False,
            tools=registry,
            tracker_info=None,
            config=cfg,
            auto_approve=True,
            active_plan_id=executing.plan_id,
            active_plan_revision=executing.approved_revision,
            _plan_execution_ready=False,
        )
        denied = await ToolExecutor(agent).execute_single_tool(
            {
                "tool_id": "write-before-resume",
                "tool_name": "write_file",
                "arguments": {"path": "blocked.txt", "content": "no"},
                "parse_error": None,
            },
            hooks_data=None,
            hooks_manager=None,
        )

    assert denied["success"] is False
    assert "explicitly resumed" in denied["error"]
    assert not (tmp_path / "blocked.txt").exists()
