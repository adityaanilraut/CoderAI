"""Approval Service — port of dsh interaction/user-approval subsystem.

Provides a structured approval seam for tool execution permissions:
1. Fail-closed: returns 'unavailable' or 'rejected' if no answerer approves.
2. Per-session policy ('ask' vs 'never'). 'never' deterministically rejects without asking.
3. Emits audit events (approval/asked and approval/decided) to the session log.
4. Clean separation between scope evaluation and approval dispatch.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from coderai.core.session import SessionManager


class ApprovalOutcome(str, enum.Enum):
    """Closed set of approval outcomes (fail-closed)."""

    ALLOWED_ONCE = "allowed-once"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class ApprovalPolicy(str, enum.Enum):
    """Session-level approval policy."""

    ASK = "ask"
    NEVER = "never"


@dataclass
class ApprovalRequest:
    """Permission question for a specific tool action."""

    tool_name: str
    session_id: str
    call_id: str | None = None
    reason: str | None = None
    scopes: list[str] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: f"apr_{uuid.uuid4().hex[:8]}")


class ApprovalService:
    """Dispatches approval requests, tracks session policy, and emits audit events."""

    def __init__(
        self,
        manager: SessionManager | None = None,
        default_policy: ApprovalPolicy = ApprovalPolicy.ASK,
    ) -> None:
        self.manager = manager
        self.default_policy = default_policy
        self._session_policies: dict[str, ApprovalPolicy] = {}
        self._answerers: list[Callable[[ApprovalRequest], ApprovalOutcome | None]] = []

    def set_policy(self, session_id: str, policy: ApprovalPolicy | str) -> None:
        if isinstance(policy, str):
            policy = ApprovalPolicy(policy.lower())
        self._session_policies[session_id] = policy

    def get_policy(self, session_id: str) -> ApprovalPolicy:
        return self._session_policies.get(session_id, self.default_policy)

    def register_answerer(
        self, answerer: Callable[[ApprovalRequest], ApprovalOutcome | None]
    ) -> None:
        """Register a custom decision provider (e.g. CLI prompt or ACP bridge)."""
        self._answerers.append(answerer)

    def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        """Evaluate approval for a tool action with fail-closed semantics."""
        from coderai.core.events import make_approval_asked, make_approval_decided

        policy = self.get_policy(req.session_id)

        # Emit approval/asked audit event if manager available
        if self.manager:
            seq = self.manager._next_seq(req.session_id)
            self.manager._append_event(
                req.session_id,
                make_approval_asked(
                    seq,
                    req.request_id,
                    req.tool_name,
                    call_id=req.call_id,
                    reason=req.reason,
                ),
            )

        # If policy is 'never', deterministically reject without dispatching answerers
        if policy == ApprovalPolicy.NEVER:
            outcome = ApprovalOutcome.REJECTED
        else:
            outcome = ApprovalOutcome.UNAVAILABLE
            for answerer in self._answerers:
                try:
                    result = answerer(req)
                    if result is not None:
                        outcome = result
                        break
                except Exception:
                    continue

        # Emit approval/decided audit event
        if self.manager:
            seq = self.manager._next_seq(req.session_id)
            self.manager._append_event(
                req.session_id,
                make_approval_decided(
                    seq,
                    req.request_id,
                    outcome.value if isinstance(outcome, ApprovalOutcome) else str(outcome),
                ),
            )

        return outcome
