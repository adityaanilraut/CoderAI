"""Thin TUI command adapter: compatibility exports plus command dispatch."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from collections.abc import Awaitable, Callable

from coderAI.application.tui_mcp_service import (  # noqa: F401
    _cmd_invoke_mcp_prompt,
    _cmd_list_mcp_servers,
    _cmd_toggle_mcp_server,
)
from coderAI.application.tui_plan_service import (  # noqa: F401
    _cmd_amend_plan,
    _cmd_answer_plan,
    _cmd_apply_plan,
    _cmd_approve_plan,
    _cmd_cancel_plan,
    _cmd_edit_plan,
    _cmd_get_plan,
    _cmd_resume_plan,
    _cmd_start_plan,
    _emit_plan,
    _execute_plan,
    _plan_store,
    _run_planning_turn,
)
from coderAI.application.tui_project_service import _cmd_init_project, _do_init_project  # noqa: F401
from coderAI.application.tui_reference_service import (  # noqa: F401
    _MAX_CHARS,
    _build_config_text,
    _build_cost_text,
    _build_info_text,
    _build_models_text,
    _build_system_text,
    _build_tasks_text,
    _cmd_list_models,
    _cmd_reference,
    _cmd_search_codebase,
    _cmd_set_default_model,
    _flatten_model_info,
    _mask_keys,
    _resolve_reference_text,
    _truncate,
)
from coderAI.application.tui_session_service import (  # noqa: F401
    _cmd_cancel,
    _cmd_cancel_agent,
    _cmd_clear_context,
    _cmd_compact_context,
    _cmd_exit,
    _cmd_export_session,
    _cmd_fork_session,
    _cmd_get_state,
    _cmd_get_tasks,
    _cmd_manage_context,
    _cmd_rewind,
    _cmd_send_message,
    _cmd_tool_approval_resp,
    _emit_context_state,
    _tracker,
)
from coderAI.application.tui_settings_service import (  # noqa: F401
    _approval_rules,
    _cmd_allow_tool,
    _cmd_disallow_tool,
    _cmd_list_allowed_tools,
    _cmd_list_personas,
    _cmd_list_skills,
    _cmd_set_auto_approve,
    _cmd_set_model,
    _cmd_set_persona,
    _cmd_set_reasoning,
    _cmd_set_verbosity,
    _cmd_toggle_auto_approve,
    _cmd_trust,
    _handle_persona_slash,
)

if TYPE_CHECKING:
    from coderAI.tui.controller import UIBridge

logger = logging.getLogger(__name__)


def _document_adapter_event_contract(emit: Callable[..., Any]) -> None:
    """Keep adapter-owned formatted event names visible to the contract scanner."""
    if TYPE_CHECKING:
        emit("available_mcp_servers")
        emit("available_models")
        emit("available_personas")
        emit("available_skills")
        emit("context_state")
        emit("plan_card")
        emit("session_patch")
        emit("success")


_COMMAND_HANDLERS: dict[str, Callable[["UIBridge", dict[str, Any]], Awaitable[None]]] = {
    "send_message": _cmd_send_message,
    "trust": _cmd_trust,
    "allow_tool": _cmd_allow_tool,
    "disallow_tool": _cmd_disallow_tool,
    "list_allowed_tools": _cmd_list_allowed_tools,
    "cancel": _cmd_cancel,
    "cancel_agent": _cmd_cancel_agent,
    "set_model": _cmd_set_model,
    "set_reasoning": _cmd_set_reasoning,
    "set_persona": _cmd_set_persona,
    "toggle_auto_approve": _cmd_toggle_auto_approve,
    "set_auto_approve": _cmd_set_auto_approve,
    "tool_approval_resp": _cmd_tool_approval_resp,
    "clear_context": _cmd_clear_context,
    "rewind": _cmd_rewind,
    "fork_session": _cmd_fork_session,
    "export_session": _cmd_export_session,
    "compact_context": _cmd_compact_context,
    "manage_context": _cmd_manage_context,
    "get_state": _cmd_get_state,
    "get_tasks": _cmd_get_tasks,
    "start_plan": _cmd_start_plan,
    "get_plan": _cmd_get_plan,
    "amend_plan": _cmd_amend_plan,
    "answer_plan": _cmd_answer_plan,
    "edit_plan": _cmd_edit_plan,
    "apply_plan": _cmd_apply_plan,
    "approve_plan": _cmd_approve_plan,
    "resume_plan": _cmd_resume_plan,
    "cancel_plan": _cmd_cancel_plan,
    "list_models": _cmd_list_models,
    "list_personas": _cmd_list_personas,
    "list_skills": _cmd_list_skills,
    "list_mcp_servers": _cmd_list_mcp_servers,
    "toggle_mcp_server": _cmd_toggle_mcp_server,
    "invoke_mcp_prompt": _cmd_invoke_mcp_prompt,
    "search_codebase": _cmd_search_codebase,
    "reference": _cmd_reference,
    "set_default_model": _cmd_set_default_model,
    "set_verbosity": _cmd_set_verbosity,
    "init_project": _cmd_init_project,
    "exit": _cmd_exit,
}
