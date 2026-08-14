"""Scriptable, non-interactive Plan Mode lifecycle."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from collections.abc import Iterator

import click

from coderAI.core.planning import (
    PlanRecord,
    PlanStore,
    build_execution_prompt,
    render_plan_markdown,
)
from coderAI.core.services import services_scope
from coderAI.system.config import Config
from coderAI.tools.planning import SubmitPlanTool

from .run_cmd import _build_agent
from .utils import missing_api_key_message


def _config() -> Config:
    cfg = Config(project_root=str(Path.cwd().resolve()))
    return cfg


def _payload(store: PlanStore, record: PlanRecord) -> dict[str, Any]:
    return {
        "success": True,
        "plan_id": record.plan_id,
        "revision": record.revision,
        "status": record.status,
        "approved_revision": record.approved_revision,
        "editable_path": str(store.draft_path(record)),
        "unanswered_questions": [
            question.model_dump() for question in record.proposal.pending_questions
        ],
        "amendments": store.revision_history(record),
        "approvals": [item.model_dump() for item in record.approvals],
        "executions": [item.model_dump() for item in record.executions],
        "proposal": record.proposal.model_dump(),
    }


def _emit(store: PlanStore, record: PlanRecord, *, json_output: bool) -> None:
    if json_output:
        click.echo(json.dumps(_payload(store, record), separators=(",", ":")))
    else:
        click.echo(render_plan_markdown(record))
        click.echo(f"\nEditable artifact: {store.draft_path(record)}")


def _active(store: PlanStore) -> PlanRecord:
    record = store.load_active()
    if record is None:
        raise click.ClickException("No active plan in this project.")
    return record


@contextmanager
def _plan_errors() -> Iterator[None]:
    try:
        yield
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


async def _create_plan(
    request: str,
    *,
    model: Optional[str],
    persona: Optional[str],
    max_iterations: Optional[int],
) -> tuple[PlanStore, PlanRecord]:
    agent = _build_agent(
        model=model,
        persona=persona,
        auto_approve=False,
        resume=None,
        resume_latest=False,
        max_iterations=max_iterations,
    )
    try:
        tool = agent.tools.get("submit_plan")
        if not isinstance(tool, SubmitPlanTool):
            raise click.ClickException("Plan Mode is unavailable: submit_plan is not registered.")
        tool.last_proposal = None
        agent.plan_mode = True
        agent._cached_system_prompt = None
        agent._refresh_session_system_prompt()
        prompt = (
            "Create a decision-complete implementation plan for this request:\n\n"
            f"{request}\n\n"
            "Inspect the repository with read-only tools, then call submit_plan exactly once. "
            "Do not implement any part of the plan. Give every user decision a stable question ID."
        )
        result = await agent.process_message(prompt)
        if not bool((result or {}).get("success")) or tool.last_proposal is None:
            raise click.ClickException("Planning ended without a valid structured proposal.")
        with services_scope(config=agent.config):
            store = PlanStore(agent.config.project_root)
            session_id = getattr(getattr(agent, "session", None), "session_id", None)
            record = store.create(request, tool.last_proposal, source_session_id=session_id)
        return store, record
    finally:
        agent.plan_mode = False
        await agent.close()


async def _execute(
    record: PlanRecord,
    *,
    model: Optional[str],
    resume_session: Optional[str],
    auto_approve: bool,
    max_iterations: Optional[int],
) -> tuple[PlanStore, PlanRecord, dict[str, Any], list[str]]:
    blocked_tools: list[str] = []
    agent = _build_agent(
        model=model,
        persona=None,
        auto_approve=auto_approve,
        resume=resume_session,
        resume_latest=False,
        max_iterations=max_iterations,
    )
    if not agent.auto_approve:
        from coderAI.core.ports import DenyByDefaultApprovalPort

        agent.approval_port = DenyByDefaultApprovalPort(on_denied=blocked_tools.append)
        agent._configure_delegate_tool_context()

    try:
        with services_scope(config=agent.config):
            store = PlanStore(agent.config.project_root)
            current = store.load(record.plan_id)
            if current is None:
                raise click.ClickException("Plan record could not be loaded in this project.")
            proposal = store.load_revision(current, current.approved_revision or current.revision)
            session_id = getattr(getattr(agent, "session", None), "session_id", None)
            executing = store.mark_executing(
                current,
                execution_session_id=session_id,
                resume=current.status in {"executing", "paused"},
            )
            agent.set_active_plan_execution(
                executing.plan_id, executing.approved_revision or executing.revision
            )
            try:
                result = await agent.process_message(build_execution_prompt(executing, proposal))
            except Exception:
                latest = store.load(executing.plan_id)
                if (
                    latest is not None
                    and latest.revision == executing.revision
                    and latest.status == "executing"
                ):
                    store.mark_finished(latest, success=False, stop_reason="error")
                raise
            latest = store.load(executing.plan_id)
            if latest is None:
                raise click.ClickException("Plan record disappeared during execution.")
            if latest.revision == executing.revision and latest.status == "executing":
                if blocked_tools:
                    latest = store.mark_paused(latest, stop_reason="permission_denied")
                else:
                    latest = store.mark_finished(
                        latest,
                        success=bool((result or {}).get("success")),
                        stop_reason=str((result or {}).get("stop_reason") or "unknown"),
                    )
            return store, latest, result, blocked_tools
    finally:
        agent.plan_mode = False
        agent.clear_active_plan_execution()
        await agent.close()


@click.group("plan")
def plan() -> None:
    """Create, review, edit, approve, and execute versioned plans."""


@plan.command("create")
@click.argument("request")
@click.option("--model", "-m")
@click.option("--persona", "-p")
@click.option("--max-iterations", type=int, default=None)
@click.option("--json", "json_output", is_flag=True)
def create(
    request: str,
    model: Optional[str],
    persona: Optional[str],
    max_iterations: Optional[int],
    json_output: bool,
) -> None:
    """Explore read-only and create a structured draft."""
    if key_error := missing_api_key_message(model):
        raise click.ClickException(key_error)
    with _plan_errors():
        store, record = asyncio.run(
            _create_plan(
                request,
                model=model,
                persona=persona,
                max_iterations=max_iterations,
            )
        )
    _emit(store, record, json_output=json_output)


@plan.command("show")
@click.option("--json", "json_output", is_flag=True)
def show(json_output: bool) -> None:
    """Show the active plan and its durable history."""
    with _plan_errors(), services_scope(config=_config()):
        store = PlanStore(str(Path.cwd()))
        _emit(store, _active(store), json_output=json_output)


@plan.command("edit")
@click.option("--reset", is_flag=True, help="Discard unapplied edits")
def edit(reset: bool) -> None:
    """Print the validated mutable artifact path."""
    with _plan_errors(), services_scope(config=_config()):
        store = PlanStore(str(Path.cwd()))
        record = _active(store)
        path = store.refresh_draft(record) if reset else store.draft_path(record)
        click.echo(path)


@plan.command("apply")
@click.option("--file", "path", default=None, help="Alternate project-scoped draft JSON")
@click.option("--json", "json_output", is_flag=True)
def apply(path: Optional[str], json_output: bool) -> None:
    """Validate an edited artifact and create an immutable revision."""
    with _plan_errors(), services_scope(config=_config()):
        store = PlanStore(str(Path.cwd()))
        revised = store.apply_draft(_active(store), path)
        _emit(store, revised, json_output=json_output)


@plan.command("answer")
@click.argument("question_id")
@click.argument("answer")
@click.option("--json", "json_output", is_flag=True)
def answer(question_id: str, answer: str, json_output: bool) -> None:
    """Set or replace one structured answer without another model turn."""
    with _plan_errors(), services_scope(config=_config()):
        store = PlanStore(str(Path.cwd()))
        revised = store.answer_question(_active(store), question_id, answer)
        _emit(store, revised, json_output=json_output)


@plan.command("approve")
@click.option("--json", "json_output", is_flag=True)
def approve(json_output: bool) -> None:
    """Approve the exact current immutable revision without executing it."""
    with _plan_errors(), services_scope(config=_config()):
        store = PlanStore(str(Path.cwd()))
        approved = store.approve(_active(store))
        _emit(store, approved, json_output=json_output)


@plan.command("execute")
@click.option("--model", "-m")
@click.option("--resume", "resume_session", default=None, help="Resume a conversation session")
@click.option("--auto-approve", "--yolo", is_flag=True, help="Allow implementation mutations")
@click.option("--max-iterations", type=int, default=None)
@click.option("--json", "json_output", is_flag=True)
def execute(
    model: Optional[str],
    resume_session: Optional[str],
    auto_approve: bool,
    max_iterations: Optional[int],
    json_output: bool,
) -> None:
    """Execute an approved revision, or resume an interrupted executing one."""
    if key_error := missing_api_key_message(model):
        raise click.ClickException(key_error)
    with _plan_errors(), services_scope(config=_config()):
        store = PlanStore(str(Path.cwd()))
        record = _active(store)
        if record.status not in {"approved", "executing", "paused"}:
            raise click.ClickException(
                f"Plan is {record.status}; run 'coderAI plan approve' after review."
            )
    with _plan_errors():
        store, finished, result, blocked = asyncio.run(
            _execute(
                record,
                model=model,
                resume_session=resume_session,
                auto_approve=auto_approve,
                max_iterations=max_iterations,
            )
        )
    payload = _payload(store, finished)
    payload.update(
        {
            "response": str((result or {}).get("content") or ""),
            "blocked_tools": sorted(set(blocked)),
        }
    )
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":")))
    else:
        click.echo(payload["response"])
        click.echo(f"Plan {finished.plan_id[:8]} r{finished.revision}: {finished.status}")
    if blocked or finished.status != "completed":
        raise click.exceptions.Exit(1)
