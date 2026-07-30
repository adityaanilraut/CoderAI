"""Immutable run context for session-scoped execution and delegation."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

if TYPE_CHECKING:
    from coderAI.core.workspace_transactions import WorkspaceTransactionStore
    from coderAI.tools.undo import FileBackupStore

IsolationDomain = Literal["auto", "read_only", "browser", "desktop", "workspace"]

# Domains that may run concurrently when mutating sub-agents are fanned out.
PARALLEL_MUTATING_DOMAINS = frozenset({"browser"})


@dataclass(frozen=True)
class PermissionPolicySnapshot:
    """Permission inputs pinned to an agent run.

    The effective tool set may be narrowed further by personas or delegation,
    but a child must never widen these pinned capabilities.
    """

    auto_approve: bool = False
    workspace_trusted: bool = False
    allowed_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RunContext:
    """Identity and session-scoped stores for one agent run.

    A context is replaced, never mutated, when an explicit lifecycle event
    supplies a session or tracker identity. Recovery and transaction stores are
    therefore selected by the owning session rather than ambient global state.
    """

    run_id: str = "unbound"
    session_id: Optional[str] = None
    agent_id: str = "main"
    workspace_id: str = "unbound"
    workspace_root: Optional[str] = None
    checkpoint_store: Optional["FileBackupStore"] = None
    transaction_store: Optional["WorkspaceTransactionStore"] = None
    permission_policy: PermissionPolicySnapshot = PermissionPolicySnapshot()
    isolation_domain: Optional[IsolationDomain] = None


# Compatibility name for integrations that imported the Milestone 1 type.
AgentExecutionContext = RunContext


def create_run_context(
    *,
    workspace_root: str,
    permission_policy: Optional[PermissionPolicySnapshot] = None,
) -> RunContext:
    """Create a fresh immutable context for an Agent lifetime."""
    resolved = str(Path(workspace_root).expanduser().resolve())
    workspace_id = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return RunContext(
        run_id=f"run_{uuid.uuid4().hex}",
        workspace_id=workspace_id,
        workspace_root=resolved,
        permission_policy=permission_policy or PermissionPolicySnapshot(),
    )


_execution_context: ContextVar[RunContext] = ContextVar(
    "agent_execution_context",
    default=RunContext(),
)


def get_execution_context() -> RunContext:
    return _execution_context.get()


def set_execution_context(ctx: RunContext) -> Token:
    return _execution_context.set(ctx)


def reset_execution_context(token: Token) -> None:
    _execution_context.reset(token)


@contextmanager
def execution_context_scope(
    agent_id: Optional[str] = None,
    isolation_domain: Optional[IsolationDomain] = None,
    *,
    run_context: Optional[RunContext] = None,
) -> Iterator[RunContext]:
    """Temporarily bind a run context for tool execution.

    Existing callers may still pass only an agent id.  In that case all other
    fields are inherited from the current context rather than silently losing
    the owning session's checkpoint store.
    """
    base = run_context or get_execution_context()
    ctx = replace(
        base,
        agent_id=agent_id if agent_id is not None else base.agent_id,
        isolation_domain=(
            isolation_domain if isolation_domain is not None else base.isolation_domain
        ),
    )
    token = set_execution_context(ctx)
    try:
        yield ctx
    finally:
        reset_execution_context(token)


def resolve_delegation_isolation_domain(arguments: Optional[dict[str, Any]]) -> IsolationDomain:
    """Map ``delegate_task`` arguments to an executor routing domain."""
    if not isinstance(arguments, dict):
        return "workspace"
    if bool(arguments.get("read_only_task")):
        return "read_only"
    domain = arguments.get("isolation_domain", "auto")
    if domain == "read_only":
        return "read_only"
    if domain in PARALLEL_MUTATING_DOMAINS:
        # Members of PARALLEL_MUTATING_DOMAINS are themselves IsolationDomain
        # values, so the membership check guarantees a valid literal here.
        return cast(IsolationDomain, domain)
    if domain == "desktop":
        return "desktop"
    # ``auto`` and ``workspace`` default to conservative workspace serialization.
    return "workspace"
