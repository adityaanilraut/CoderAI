"""Per-agent execution context for tool isolation during parallel delegation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Literal, Optional

IsolationDomain = Literal["auto", "read_only", "browser", "desktop", "workspace"]

# Domains that may run concurrently when mutating sub-agents are fanned out.
PARALLEL_MUTATING_DOMAINS = frozenset({"browser"})


@dataclass(frozen=True)
class AgentExecutionContext:
    """Snapshot of the agent currently executing a tool call."""

    agent_id: str = "main"
    isolation_domain: Optional[IsolationDomain] = None


_execution_context: ContextVar[AgentExecutionContext] = ContextVar(
    "agent_execution_context",
    default=AgentExecutionContext(),
)


def get_execution_context() -> AgentExecutionContext:
    return _execution_context.get()


def set_execution_context(ctx: AgentExecutionContext) -> Token:
    return _execution_context.set(ctx)


def reset_execution_context(token: Token) -> None:
    _execution_context.reset(token)


@contextmanager
def execution_context_scope(
    agent_id: str,
    isolation_domain: Optional[IsolationDomain] = None,
) -> Iterator[AgentExecutionContext]:
    """Temporarily bind ``agent_id`` (and optional domain) for tool execution."""
    ctx = AgentExecutionContext(agent_id=agent_id, isolation_domain=isolation_domain)
    token = set_execution_context(ctx)
    try:
        yield ctx
    finally:
        reset_execution_context(token)


def resolve_delegation_isolation_domain(arguments: Optional[Dict[str, Any]]) -> str:
    """Map ``delegate_task`` arguments to an executor routing domain."""
    if not isinstance(arguments, dict):
        return "workspace"
    if bool(arguments.get("read_only_task")):
        return "read_only"
    domain = arguments.get("isolation_domain", "auto")
    if domain == "read_only":
        return "read_only"
    if domain in PARALLEL_MUTATING_DOMAINS:
        return str(domain)
    if domain == "desktop":
        return "desktop"
    # ``auto`` and ``workspace`` default to conservative workspace serialization.
    return "workspace"
