"""Bootstrap Agent + UIBridge for the Textual UI.

The Agent/session construction (flag resolution, load-vs-create, delegate
wiring) lives in :mod:`coderAI.core.session_bootstrap`; this module only layers
the TUI-specific bridge, streaming handler, and tracker registration on top.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from collections.abc import Callable

from coderAI.core.agent import Agent
from coderAI.core.agent_tracker import AgentStatus, agent_tracker
from coderAI.core.session_bootstrap import bootstrap_agent
from .controller import UIBridge
from .streaming import BridgeStreamingHandler

logger = logging.getLogger(__name__)


def create_agent_session(
    *,
    model: Optional[str] = None,
    resume: Optional[str] = None,
    continue_: bool = False,
    auto_approve: bool = False,
    persona: Optional[str] = None,
    on_event: Callable[[str, dict[str, Any]], None],
) -> tuple[Agent, UIBridge]:
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
    controller = UIBridge(agent=agent, on_event=on_event)
    agent.approval_port = controller

    agent.tracker_info = agent_tracker.register(
        name=agent.persona.name if agent.persona else "main",
        role=agent.persona.description if agent.persona else None,
        model=agent.model,
        context_limit=agent.get_context_limit(),
    )
    agent.tracker_info.status = AgentStatus.IDLE
    agent._tracker_start_completion = agent.total_completion_tokens
    agent._tracker_start_tokens = agent.total_tokens
    agent._tracker_start_cost = agent.cost_tracker.get_total_cost()

    agent._configure_delegate_tool_context()
    agent.streaming_handler = BridgeStreamingHandler(controller)

    # MCP elicitation: reuse the tool-approval modal (accept / decline).
    async def _mcp_elicitation_handler(server_name: str, params: dict[str, Any]) -> dict[str, Any]:
        import uuid

        message = ""
        if isinstance(params, dict):
            message = str(params.get("message") or params.get("prompt") or "")
        tool_id = f"mcp-elicit-{uuid.uuid4().hex[:12]}"
        try:
            approved = await controller.request_tool_approval(
                tool_id=tool_id,
                tool_name=f"mcp_elicitation:{server_name}",
                arguments={"message": message, "params": params},
            )
        except Exception:
            logger.debug("MCP elicitation approval failed", exc_info=True)
            return {"action": "cancel", "content": {}}
        if approved:
            return {"action": "accept", "content": {}}
        return {"action": "decline", "content": {}}

    from coderAI.tools.mcp import mcp_client

    mcp_client.elicitation_handler = _mcp_elicitation_handler
    return agent, controller
