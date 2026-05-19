"""Bootstrap Agent + UIBridge for the Textual UI."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

from coderAI.core.agent import Agent
from coderAI.core.agent_tracker import AgentStatus, agent_tracker
from coderAI.system.history import history_manager
from ..bridge.controller import UIBridge
from ..bridge.streaming import BridgeStreamingHandler

logger = logging.getLogger(__name__)


def _activate_resumed_session_model(agent: Agent, requested_model: Optional[str]) -> None:
    session = getattr(agent, "session", None)
    if session is None:
        return
    effective_model = requested_model or session.model or agent.model
    if agent.model != effective_model:
        agent.model = effective_model
        agent.provider = agent._create_provider()
        agent.context_controller.provider = agent.provider
    session.model = effective_model
    agent.realign_provider_usage_counters()
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
    if continue_ and not resume:
        resume = history_manager.get_latest_session_id()

    agent = Agent(
        model=model,
        streaming=True,
        auto_approve=auto_approve,
        persona_name=persona,
    )

    if resume:
        try:
            session = agent.load_session(resume)
        except Exception:
            session = None
            logger.exception("Failed to load session %s; starting fresh", resume)
        if session is None:
            agent.create_session()
        else:
            _activate_resumed_session_model(agent, model)
    else:
        agent.create_session()

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
