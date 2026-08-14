"""Plan creation, revision, approval, and execution application service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coderAI.tui.controller import UIBridge

logger = logging.getLogger(__name__)


def _emit_plan(server: UIBridge, record: Any) -> None:
    from coderAI.core.planning import render_plan_markdown

    store = _plan_store(server)
    server.emit(
        "plan_card",
        planId=record.plan_id,
        revision=record.revision,
        status=record.status,
        markdown=render_plan_markdown(record, include_questions=False),
        questions=[question.model_dump() for question in record.proposal.questions],
        unansweredQuestions=list(record.proposal.unanswered_questions),
        editablePath=str(store.draft_path(record)),
        approvals=[item.model_dump() for item in record.approvals],
        executions=[item.model_dump() for item in record.executions],
        amendments=store.revision_history(record),
    )


def _plan_store(server: UIBridge) -> Any:
    from coderAI.core.planning import PlanStore

    return PlanStore(getattr(server.agent.config, "project_root", ".") or ".")


async def _run_planning_turn(
    server: UIBridge,
    *,
    objective: str,
    planning_prompt: str,
    current: Any = None,
    amendment: str = "",
) -> None:
    from coderAI.tools.planning import SubmitPlanTool

    async with server._turn_lock:
        tool = server.agent.tools.get("submit_plan")
        if not isinstance(tool, SubmitPlanTool):
            server.emit(
                "warning", message="Plan Mode is unavailable: submit_plan is not registered."
            )
            server.emit_ready()
            return
        tool.last_proposal = None
        server.agent.plan_mode = True
        server.agent._cached_system_prompt = None
        server.agent._refresh_session_system_prompt()
        server.tick_iteration()
        try:
            result = await server.agent.process_message(planning_prompt)
            if not getattr(server.agent, "streaming", True):
                content = str((result or {}).get("content", "") or "")
                server.emit("turn", phase="start", reasoningActive=False)
                if content:
                    server.emit("turn", phase="text", delta=content, reasoningActive=False)
                server.emit("turn", phase="end", reasoningActive=False)
            if not bool((result or {}).get("success")):
                server.emit(
                    "warning",
                    message="Planning did not complete cleanly; no plan revision was saved.",
                )
                return
        except Exception as e:
            logger.exception("planning turn failed")
            server._emit_error("internal", str(e), hint="The draft plan was not saved.")
            return
        finally:
            server.agent.plan_mode = False
            server.agent._cached_system_prompt = None
            server.agent._refresh_session_system_prompt()
            server.emit_status()
            server.emit_ready()

        proposal = tool.last_proposal
        if proposal is None:
            server.emit(
                "warning",
                message="The planning turn ended without a structured plan. Run /plan amend with guidance.",
            )
            return
        store = _plan_store(server)
        if current is None:
            session = getattr(server.agent, "session", None)
            record = store.create(
                objective,
                proposal,
                source_session_id=getattr(session, "session_id", None),
            )
        else:
            record = store.revise(current, proposal, amendment)
        _emit_plan(server, record)


async def _cmd_start_plan(server: UIBridge, msg: dict[str, Any]) -> None:
    request = str(msg.get("request") or "").strip()
    if not request:
        server.emit("warning", message="Usage: /plan <request>")
        return
    prompt = (
        "Create a decision-complete implementation plan for this request:\n\n"
        f"{request}\n\n"
        "First inspect the repository with read-only tools. Resolve discoverable facts yourself. "
        "Then call submit_plan exactly once. Do not implement any part of the plan."
    )
    await _run_planning_turn(server, objective=request, planning_prompt=prompt)


async def _cmd_get_plan(server: UIBridge, _msg: dict[str, Any]) -> None:
    record = _plan_store(server).load_active()
    if record is None:
        server.emit("info", message="No active plan. Start one with /plan <request>.")
        return
    _emit_plan(server, record)


async def _cmd_amend_plan(server: UIBridge, msg: dict[str, Any]) -> None:
    instruction = str(msg.get("instruction") or "").strip()
    store = _plan_store(server)
    current = store.load_active()
    if current is None:
        server.emit("warning", message="No active plan to amend. Start one with /plan <request>.")
        return
    if current.status in {"executing", "completed"}:
        server.emit(
            "warning", message=f"Plan is already {current.status}; start a new plan instead."
        )
        return
    from coderAI.core.planning import render_plan_markdown

    prompt = (
        "Revise the existing plan using the user's amendment. Re-inspect repository facts when "
        "needed, preserve valid decisions, and call submit_plan exactly once. Do not implement.\n\n"
        f"Existing plan:\n{render_plan_markdown(current)}\n\n"
        f"Amendment or answer:\n{instruction}"
    )
    await _run_planning_turn(
        server,
        objective=current.objective,
        planning_prompt=prompt,
        current=current,
        amendment=instruction,
    )


async def _cmd_answer_plan(server: UIBridge, msg: dict[str, Any]) -> None:
    question_id = str(msg.get("questionId") or "").strip()
    answer = str(msg.get("answer") or "").strip()
    store = _plan_store(server)
    current = store.load_active()
    if current is None:
        server.emit("warning", message="No active plan to answer.")
        return
    try:
        revised = store.answer_question(current, question_id, answer)
    except ValueError as exc:
        server.emit("warning", message=str(exc))
        _emit_plan(server, current)
        return
    _emit_plan(server, revised)


async def _cmd_edit_plan(server: UIBridge, msg: dict[str, Any]) -> None:
    store = _plan_store(server)
    current = store.load_active()
    if current is None:
        server.emit("warning", message="No active plan to edit.")
        return
    if bool(msg.get("reset")):
        try:
            store.refresh_draft(current)
        except ValueError as exc:
            server.emit("warning", message=str(exc))
            return
    path = store.draft_path(current)
    server.emit(
        "info",
        message=(
            f"Editable plan artifact: {path}\n"
            "Edit proposal fields, set a concise amendment description, then run /plan apply. "
            "Immutable revision files are never edited in place."
        ),
    )
    _emit_plan(server, current)


async def _cmd_apply_plan(server: UIBridge, msg: dict[str, Any]) -> None:
    store = _plan_store(server)
    current = store.load_active()
    if current is None:
        server.emit("warning", message="No active plan draft to apply.")
        return
    path = str(msg.get("path") or "").strip() or None
    try:
        revised = store.apply_draft(current, path)
    except ValueError as exc:
        server.emit("warning", message=str(exc))
        _emit_plan(server, current)
        return
    _emit_plan(server, revised)


async def _cmd_cancel_plan(server: UIBridge, _msg: dict[str, Any]) -> None:
    store = _plan_store(server)
    current = store.load_active()
    if current is None:
        server.emit("info", message="No active plan to cancel.")
        return
    if current.status in {"executing", "completed"}:
        server.emit(
            "warning", message=f"Plan cannot be cancelled while status is {current.status}."
        )
        return
    _emit_plan(server, store.cancel(current))


async def _cmd_approve_plan(server: UIBridge, _msg: dict[str, Any]) -> None:
    store = _plan_store(server)
    current = store.load_active()
    if current is None:
        server.emit("warning", message="No active plan to approve.")
        return
    try:
        approved = store.approve(current)
    except ValueError as exc:
        server.emit("warning", message=str(exc))
        _emit_plan(server, current)
        return
    await _execute_plan(server, approved, resume=False)


async def _cmd_resume_plan(server: UIBridge, _msg: dict[str, Any]) -> None:
    current = _plan_store(server).load_active()
    if current is None:
        server.emit("warning", message="No active plan execution to resume.")
        return
    if current.status not in {"executing", "paused"}:
        server.emit(
            "warning",
            message=f"Plan is {current.status}; only paused or executing plans can resume.",
        )
        _emit_plan(server, current)
        return
    await _execute_plan(server, current, resume=True)


async def _execute_plan(server: UIBridge, current: Any, *, resume: bool) -> None:
    from coderAI.core.planning import build_execution_prompt

    async with server._turn_lock:
        store = _plan_store(server)
        session = getattr(server.agent, "session", None)
        session_id = getattr(session, "session_id", None)
        try:
            proposal = store.load_revision(current, current.approved_revision or current.revision)
            executing = store.mark_executing(
                current, execution_session_id=session_id, resume=resume
            )
        except ValueError as exc:
            server.emit("warning", message=str(exc))
            _emit_plan(server, current)
            return
        _emit_plan(server, executing)
        set_execution = getattr(server.agent, "set_active_plan_execution", None)
        if callable(set_execution):
            set_execution(executing.plan_id, executing.approved_revision or executing.revision)
        else:
            server.agent.active_plan_id = executing.plan_id
            server.agent.active_plan_revision = executing.approved_revision or executing.revision
            server.agent._plan_execution_ready = True
        server.agent.plan_mode = False
        server.tick_iteration()
        try:
            result = await server.agent.process_message(build_execution_prompt(executing, proposal))
            if not getattr(server.agent, "streaming", True):
                content = str((result or {}).get("content", "") or "")
                server.emit("turn", phase="start", reasoningActive=False)
                if content:
                    server.emit("turn", phase="text", delta=content, reasoningActive=False)
                server.emit("turn", phase="end", reasoningActive=False)
            latest = store.load(executing.plan_id)
            if latest is None:
                raise ValueError("Plan record disappeared during execution.")
            if latest.revision != executing.revision or latest.status != "executing":
                _emit_plan(server, latest)
                server.emit(
                    "warning",
                    message=(
                        f"Execution stopped at amendment r{latest.revision}; "
                        "review and approve it before continuing."
                    ),
                )
            else:
                success = bool((result or {}).get("success"))
                stop_reason = str((result or {}).get("stop_reason") or "unknown")
                if not success and stop_reason in {"cancelled", "denied"}:
                    finished = store.mark_paused(latest, stop_reason=stop_reason)
                else:
                    finished = store.mark_finished(
                        latest,
                        success=success,
                        stop_reason=stop_reason,
                    )
                _emit_plan(server, finished)
        except Exception as e:
            logger.exception("approved plan execution failed")
            latest = store.load(executing.plan_id)
            if (
                latest is not None
                and latest.revision == executing.revision
                and latest.status == "executing"
            ):
                latest = store.mark_finished(latest, success=False, stop_reason="error")
                _emit_plan(server, latest)
            server._emit_error(
                "internal", str(e), hint="An executing plan can be resumed with /plan resume."
            )
        finally:
            server.agent.plan_mode = False
            clear_execution = getattr(server.agent, "clear_active_plan_execution", None)
            if callable(clear_execution):
                clear_execution()
            else:
                server.agent.active_plan_id = None
                server.agent.active_plan_revision = None
                server.agent._plan_execution_ready = False
            server.emit_status()
            server.emit_ready()
