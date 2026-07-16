"""Backward-compatible re-export — prefer ``coderAI.core.personas``."""

from coderAI.core.personas import *  # noqa: F403
from coderAI.core.personas import (
    AgentPersona,
    expand_persona_tools,
    get_available_personas,
    load_agent_persona,
    persona_allowed_in_context,
    resolve_persona_name,
)

__all__ = [
    "AgentPersona",
    "expand_persona_tools",
    "get_available_personas",
    "load_agent_persona",
    "persona_allowed_in_context",
    "resolve_persona_name",
]
