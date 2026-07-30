"""Deterministic headless Plan Mode lifecycle coverage."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from coderAI.cli.plan_cmd import plan
from coderAI.core.planning import PlanProposal, PlanQuestionSpec, PlanStepSpec, PlanStore
from coderAI.core.services import services_scope
from coderAI.system.config import Config
from coderAI.tools.base import ToolRegistry
from coderAI.tools.planning import SubmitPlanTool


def _proposal(*, question: bool = False) -> PlanProposal:
    return PlanProposal(
        summary="Implement parser validation.",
        success_criteria=["Parser tests pass"],
        steps=[
            PlanStepSpec(
                id="step-1",
                title="Validate parser input",
                description="Reject invalid input at the boundary.",
                files=["parser.py"],
                checks=["pytest -q tests/test_parser.py"],
            )
        ],
        questions=[
            PlanQuestionSpec(
                id="storage",
                prompt="Which storage?",
                choices=["SQLite", "Postgres"],
            )
        ]
        if question
        else [],
    )


class PlanningAgent:
    def __init__(self, root, proposal):
        self.config = Config(project_root=str(root), save_history=False)
        self.tools = ToolRegistry()
        self.tools.register(SubmitPlanTool())
        self.session = SimpleNamespace(session_id="session-plan")
        self.plan_mode = False
        self._cached_system_prompt = None
        self.proposal = proposal
        self.closed = False

    def _refresh_session_system_prompt(self):
        pass

    async def process_message(self, _prompt):
        assert self.plan_mode is True
        self.tools.get("submit_plan").last_proposal = self.proposal
        return {"success": True, "content": "planned", "stop_reason": "stop"}

    async def close(self):
        self.closed = True


class ExecutionAgent:
    def __init__(self, root, *, mutate=False, fail=False):
        self.config = Config(project_root=str(root), save_history=False)
        self.session = SimpleNamespace(session_id="session-execute", metadata={})
        self.auto_approve = False
        self.confirmation_override = None
        self.plan_mode = False
        self.active_plan_id = None
        self.active_plan_revision = None
        self._plan_execution_ready = False
        self.mutate = mutate
        self.fail = fail
        self.closed = False

    def _configure_delegate_tool_context(self):
        pass

    def set_active_plan_execution(self, plan_id, revision):
        self.active_plan_id = plan_id
        self.active_plan_revision = revision
        self._plan_execution_ready = True
        self.session.metadata["plan_execution"] = {"plan_id": plan_id, "revision": revision}

    def clear_active_plan_execution(self):
        self.active_plan_id = None
        self.active_plan_revision = None
        self._plan_execution_ready = False
        self.session.metadata.pop("plan_execution", None)

    async def process_message(self, prompt):
        assert f"plan {self.active_plan_id} revision {self.active_plan_revision}" in prompt
        if self.fail:
            raise RuntimeError("provider failed")
        if self.mutate and self.confirmation_override:
            await self.confirmation_override("write_file", {"path": "parser.py"})
        return {"success": True, "content": "implemented", "stop_reason": "stop"}

    async def close(self):
        self.closed = True


def _seed(root, proposal):
    cfg = Config(project_root=str(root))
    with services_scope(config=cfg):
        return PlanStore(str(root)).create("Fix parser", proposal, source_session_id=None)


def test_headless_create_is_read_only_and_returns_editable_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = PlanningAgent(tmp_path, _proposal(question=True))
    runner = CliRunner()
    with (
        patch("coderAI.cli.plan_cmd._build_agent", return_value=agent),
        patch("coderAI.cli.plan_cmd.missing_api_key_message", return_value=None),
    ):
        result = runner.invoke(plan, ["create", "Fix parser", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "needs_input"
    assert payload["unanswered_questions"][0]["id"] == "storage"
    assert (tmp_path / payload["editable_path"]).is_file()
    assert agent.closed is True


def test_headless_answer_approve_and_show_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = _seed(tmp_path, _proposal(question=True))
    runner = CliRunner()

    answered = runner.invoke(plan, ["answer", "storage", "SQLite", "--json"])
    approved = runner.invoke(plan, ["approve", "--json"])
    shown = runner.invoke(plan, ["show", "--json"])

    assert answered.exit_code == 0, answered.output
    assert approved.exit_code == 0, approved.output
    payload = json.loads(shown.output)
    assert payload["plan_id"] == first.plan_id
    assert payload["revision"] == 2
    assert payload["status"] == "approved"
    assert payload["approvals"][0]["revision"] == 2
    assert len(payload["amendments"]) == 2


def test_headless_execute_requires_approval_and_defaults_to_deny_mutation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = _seed(tmp_path, _proposal())
    runner = CliRunner()
    unapproved = runner.invoke(plan, ["execute", "--json"])
    assert unapproved.exit_code == 1
    assert "run 'coderAI plan approve'" in unapproved.output

    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        store.approve(store.load(first.plan_id))

    agent = ExecutionAgent(tmp_path, mutate=True)
    with (
        patch("coderAI.cli.plan_cmd._build_agent", return_value=agent),
        patch("coderAI.cli.plan_cmd.missing_api_key_message", return_value=None),
    ):
        denied = runner.invoke(plan, ["execute", "--json"])

    assert denied.exit_code == 1, denied.output
    payload = json.loads(denied.output)
    assert payload["blocked_tools"] == ["write_file"]
    assert payload["status"] == "paused"
    assert payload["approved_revision"] == 1
    assert agent.session.metadata == {}

    resumed_agent = ExecutionAgent(tmp_path)
    with (
        patch("coderAI.cli.plan_cmd._build_agent", return_value=resumed_agent),
        patch("coderAI.cli.plan_cmd.missing_api_key_message", return_value=None),
    ):
        resumed = runner.invoke(plan, ["execute", "--json"])
    assert resumed.exit_code == 0, resumed.output
    resumed_payload = json.loads(resumed.output)
    assert resumed_payload["status"] == "completed"
    assert [attempt["status"] for attempt in resumed_payload["executions"]] == [
        "blocked",
        "completed",
    ]


def test_headless_execute_can_complete_exact_approved_revision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = _seed(tmp_path, _proposal())
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        store.approve(store.load(first.plan_id))
    agent = ExecutionAgent(tmp_path)
    runner = CliRunner()
    with (
        patch("coderAI.cli.plan_cmd._build_agent", return_value=agent),
        patch("coderAI.cli.plan_cmd.missing_api_key_message", return_value=None),
    ):
        result = runner.invoke(plan, ["execute", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert payload["executions"][0]["revision"] == 1


def test_headless_execution_exception_is_failed_and_user_facing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = _seed(tmp_path, _proposal())
    cfg = Config(project_root=str(tmp_path))
    with services_scope(config=cfg):
        store = PlanStore(str(tmp_path))
        store.approve(store.load(first.plan_id))
    agent = ExecutionAgent(tmp_path, fail=True)
    runner = CliRunner()
    with (
        patch("coderAI.cli.plan_cmd._build_agent", return_value=agent),
        patch("coderAI.cli.plan_cmd.missing_api_key_message", return_value=None),
    ):
        result = runner.invoke(plan, ["execute", "--json"])

    assert result.exit_code == 1
    assert "provider failed" in result.output
    with services_scope(config=cfg):
        failed = PlanStore(str(tmp_path)).load(first.plan_id)
    assert failed is not None and failed.status == "failed"
