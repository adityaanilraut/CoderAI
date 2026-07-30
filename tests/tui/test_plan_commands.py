"""End-to-end command-handler coverage for Plan Mode lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coderAI.core.planning import PlanProposal, PlanQuestionSpec, PlanStepSpec, PlanStore
from coderAI.core.services import services_scope
from coderAI.system.config import Config
from coderAI.tools.base import ToolRegistry
from coderAI.tools.planning import SubmitPlanTool
from coderAI.tui.commands import (
    _cmd_answer_plan,
    _cmd_approve_plan,
    _cmd_resume_plan,
    _cmd_start_plan,
)


def _proposal() -> PlanProposal:
    return PlanProposal(
        summary="Implement the parser fix.",
        success_criteria=["Parser tests pass"],
        steps=[
            PlanStepSpec(
                id="step-1",
                title="Fix parser",
                description="Add boundary validation.",
                files=["parser.py"],
                checks=["pytest -q tests/test_parser.py"],
            )
        ],
        tests=["Parser regression tests"],
    )


def _question_proposal() -> PlanProposal:
    proposal = _proposal()
    proposal.questions = [
        PlanQuestionSpec(id="storage", prompt="Which storage?", choices=["SQLite", "Postgres"])
    ]
    return proposal


class FakeServer:
    def __init__(self, agent) -> None:
        self.agent = agent
        self._turn_lock = asyncio.Lock()
        self.events = []
        self.iterations = 0

    def emit(self, event, **data):
        self.events.append((event, data))

    def emit_status(self):
        self.emit("status")

    def emit_ready(self):
        self.emit("ready")

    def tick_iteration(self):
        self.iterations += 1

    def _emit_error(self, category, message, **extra):
        self.emit("error", category=category, message=message, **extra)


def _agent(tmp_path, process_message) -> SimpleNamespace:
    registry = ToolRegistry()
    registry.register(SubmitPlanTool())
    return SimpleNamespace(
        config=Config(project_root=str(tmp_path)),
        tools=registry,
        session=SimpleNamespace(session_id="session-1"),
        plan_mode=False,
        active_plan_id=None,
        active_plan_revision=None,
        streaming=True,
        process_message=process_message,
        _cached_system_prompt=None,
        _refresh_session_system_prompt=MagicMock(),
    )


@pytest.mark.asyncio
async def test_start_plan_runs_read_only_turn_and_persists_card(tmp_path):
    async def process(_prompt):
        assert agent.plan_mode is True
        agent.tools.get("submit_plan").last_proposal = _proposal()
        return {"success": True, "content": "Plan ready", "stop_reason": "stop"}

    agent = _agent(tmp_path, process)
    server = FakeServer(agent)
    with services_scope(config=agent.config):
        await _cmd_start_plan(server, {"request": "Fix the parser"})
        record = PlanStore(str(tmp_path)).load_active()

    assert agent.plan_mode is False
    assert record is not None and record.status == "draft"
    assert any(event == "plan_card" for event, _ in server.events)


@pytest.mark.asyncio
async def test_approve_plan_links_exact_revision_and_marks_completion(tmp_path):
    seen = {}

    async def process(prompt):
        seen["plan_id"] = agent.active_plan_id
        seen["revision"] = agent.active_plan_revision
        seen["prompt"] = prompt
        return {"success": True, "content": "Implemented", "stop_reason": "stop"}

    agent = _agent(tmp_path, process)
    server = FakeServer(agent)
    with services_scope(config=agent.config):
        store = PlanStore(str(tmp_path))
        draft = store.create("Fix the parser", _proposal(), source_session_id="session-1")
        await _cmd_approve_plan(server, {})
        finished = store.load(draft.plan_id)

    assert seen == {
        "plan_id": draft.plan_id,
        "revision": 1,
        "prompt": seen["prompt"],
    }
    assert f"plan {draft.plan_id} revision 1" in seen["prompt"]
    assert finished is not None and finished.status == "completed"
    assert agent.active_plan_id is None
    assert agent.active_plan_revision is None


@pytest.mark.asyncio
async def test_answer_plan_edits_structured_question_without_model_turn(tmp_path):
    process = MagicMock()
    agent = _agent(tmp_path, process)
    server = FakeServer(agent)
    with services_scope(config=agent.config):
        store = PlanStore(str(tmp_path))
        first = store.create("Choose storage", _question_proposal(), source_session_id="session-1")
        await _cmd_answer_plan(
            server,
            {"questionId": "storage", "answer": "SQLite"},
        )
        revised = store.load(first.plan_id)

    assert revised is not None
    assert revised.revision == 2
    assert revised.status == "draft"
    assert revised.proposal.questions[0].answer == "SQLite"
    process.assert_not_called()


@pytest.mark.asyncio
async def test_resume_plan_relinks_exact_approved_revision(tmp_path):
    seen = {}

    async def process(prompt):
        seen["plan_id"] = agent.active_plan_id
        seen["revision"] = agent.active_plan_revision
        return {"success": True, "content": "Resumed", "stop_reason": "stop"}

    agent = _agent(tmp_path, process)
    server = FakeServer(agent)
    with services_scope(config=agent.config):
        store = PlanStore(str(tmp_path))
        approved = store.approve(
            store.create("Fix parser", _proposal(), source_session_id="session-1")
        )
        executing = store.mark_executing(approved, execution_session_id="old-session")
        await _cmd_resume_plan(server, {})
        finished = store.load(executing.plan_id)

    assert seen == {"plan_id": executing.plan_id, "revision": 1}
    assert finished is not None and finished.status == "completed"
    assert [attempt.status for attempt in finished.executions] == ["interrupted", "completed"]


@pytest.mark.asyncio
async def test_execution_amendment_is_not_overwritten_by_old_completion(tmp_path):
    async def process(_prompt):
        store = PlanStore(str(tmp_path))
        executing = store.load_active()
        replacement = _proposal().model_copy(deep=True)
        replacement.steps[0].description = "Use the newly discovered boundary."
        store.request_execution_amendment(executing, replacement, "Approved API is unavailable")
        return {"success": True, "content": "Stopped for review", "stop_reason": "stop"}

    agent = _agent(tmp_path, process)
    server = FakeServer(agent)
    with services_scope(config=agent.config):
        store = PlanStore(str(tmp_path))
        first = store.create("Fix parser", _proposal(), source_session_id="session-1")
        await _cmd_approve_plan(server, {})
        amended = store.load(first.plan_id)

    assert amended is not None
    assert amended.revision == 2
    assert amended.status == "draft"
    assert amended.executions[0].status == "amendment_requested"
    assert any(
        event == "warning" and "review and approve" in data["message"]
        for event, data in server.events
    )
