"""Bootstrap Agent + UIBridge for the Textual UI.

The Agent/session construction (flag resolution, load-vs-create, delegate
wiring) lives in :mod:`coderAI.core.session_bootstrap`; this module only layers
the TUI-specific bridge, streaming handler, and tracker registration on top.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

from coderAI.core.agent import Agent
from coderAI.core.agent_tracker import AgentStatus, agent_tracker
from coderAI.core.session_bootstrap import bootstrap_agent
from .controller import UIBridge
from .streaming import BridgeStreamingHandler

logger = logging.getLogger(__name__)


def _activate_resumed_session_model(agent: Agent, requested_model: Optional[str]) -> None:
    session = getattr(agent, "session", None)
    if session is None:
        return
    effective_model = requested_model or session.model or agent.model
    if agent.model != effective_model:
        old_provider = agent.provider
        agent.model = effective_model
        new_provider = agent._create_provider()
        agent.provider = new_provider
        agent.context_controller.provider = new_provider
        skill_manager = getattr(agent, "skill_manager", None)
        if skill_manager is not None:
            skill_manager.provider = new_provider
        agent._close_replaced_provider(old_provider)
    session.model = effective_model
    agent._configure_delegate_tool_context()


def create_agent_session(
    *,
    model: Optional[str] = None,
    resume: Optional[str] = None,
    continue_: bool = False,
    auto_approve: bool = False,
    persona: Optional[str] = None,
    on_event: Callable[[str, Dict[str, Any]], None],
) -> Tuple[Agent, UIBridge]:
    """Create Agent and in-process UIBridge wired to ``on_event``."""
    agent = bootstrap_agent(
        model=model,
        resume_id=resume,
        continue_latest=continue_,
        streaming=True,
        auto_approve=auto_approve,
        persona=persona,
        resume_fresh_on_failure=True,
        warn=lambda message: on_event("warning", {"message": message}),
    )
    # Align the model/provider with the (possibly resumed) session. A no-op for
    # a fresh session, where session.model already equals agent.model.
    _activate_resumed_session_model(agent, model)

    controller = UIBridge(agent=agent, on_event=on_event)
    agent.ipc_server = controller
    agent._configure_delegate_tool_context()

    agent.tracker_info = agent_tracker.register(
        name=agent.persona.name if agent.persona else "main",
        role=agent.persona.description if agent.persona else None,
        model=agent.model,
        context_limit=agent.config.context_window,
    )
    agent.tracker_info.status = AgentStatus.IDLE
    agent._tracker_start_completion = agent.total_completion_tokens
    agent._tracker_start_tokens = agent.total_tokens
    agent._tracker_start_cost = agent.cost_tracker.get_total_cost()

    agent.streaming_handler = BridgeStreamingHandler(controller)
    return agent, controller
