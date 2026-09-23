"""Coordinated approval runtime (slim).

Foreground turns and background agents route approval decisions through one
registry so the UI channel sees every request exactly once. Sources carry
``(kind, id, agent_id)`` so cancellation can target a whole subtree
(``cancel_by_source``). Responses resolve an ``asyncio`` future — no wire
dependency; wire/ACP adapters live in later phases.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from typing import Any
from collections.abc import Callable
from coderai.approval_runtime.models import (
    ApprovalEvent,
    ApprovalRequestRecord,
    ApprovalResponseKind,
    ApprovalSource,
    ApprovalSourceKind,
)

_current_source: contextvars.ContextVar[ApprovalSource | None] = contextvars.ContextVar(
    "coderai_approval_source", default=None
)


def get_current_approval_source_or_none() -> ApprovalSource | None:
    """Approval source bound to this task context (if any)."""
    return _current_source.get()


def set_current_approval_source(source: ApprovalSource) -> contextvars.Token:
    """Bind an approval source for this task context; reset with the token."""
    return _current_source.set(source)


def reset_current_approval_source(token: contextvars.Token) -> None:
    """Restore the previous approval-source binding."""
    _current_source.reset(token)


class ApprovalCancelledError(Exception):
    """Raised in waiters when their request is cancelled."""


class ApprovalRuntime:
    """Single-owner registry of pending approval requests."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequestRecord] = {}
        self._waiters: dict[str, asyncio.Future[tuple[ApprovalResponseKind, str]]] = {}
        self._waiter_counts: dict[str, int] = {}
        self._subscribers: dict[str, Callable[[ApprovalEvent], None]] = {}
        self._root_wire_hub: Any | None = None

    def bind_root_wire_hub(self, root_wire_hub: Any) -> None:
        """Attach the session wire hub (approval↔wire bridge)."""
        if self._root_wire_hub is root_wire_hub:
            return
        self._root_wire_hub = root_wire_hub

    # -- lifecycle ------------------------------------------------------
    def create_request(
        self,
        *,
        tool_call_id: str,
        action: str,
        description: str,
        source: ApprovalSource,
        sender: str = "",
        display: list[Any] | None = None,
        request_id: str | None = None,
    ) -> ApprovalRequestRecord:
        """Register a request and notify subscribers; id is unique."""
        record = ApprovalRequestRecord(
            id=request_id or f"apr_{uuid.uuid4().hex[:12]}",
            tool_call_id=tool_call_id,
            action=action,
            description=description,
            source=source,
            sender=sender,
            display=list(display or []),
        )
        self._requests[record.id] = record
        self._publish(ApprovalEvent(kind="request_created", request=record))
        self._publish_wire_request(record)
        return record

    async def wait_for_response(
        self, request_id: str, timeout: float | None = None
    ) -> tuple[ApprovalResponseKind, str]:
        """Await resolution; raises ``ApprovalCancelledError`` on cancel/timeout."""
        record = self._requests.get(request_id)
        if record is None:
            raise KeyError(f"Approval request not found: {request_id}")
        waiter = self._waiters.get(request_id)
        if waiter is None:
            if record.status == "cancelled":
                raise ApprovalCancelledError(request_id)
            if record.status == "resolved":
                assert record.response is not None
                return record.response, record.feedback
            try:
                waiter = asyncio.get_running_loop().create_future()
            except RuntimeError as err:
                raise ApprovalCancelledError(f"No loop for approval request: {request_id}") from err
            self._waiters[request_id] = waiter
        self._waiter_counts[request_id] = self._waiter_counts.get(request_id, 0) + 1
        try:
            if timeout is None:
                return await asyncio.shield(waiter)
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            if self._waiter_counts.get(request_id, 0) <= 1:
                self._waiters.pop(request_id, None)
            self.cancel(request_id, feedback="approval timed out")
            raise ApprovalCancelledError(request_id) from None
        finally:
            remaining = self._waiter_counts.get(request_id, 0) - 1
            if remaining > 0:
                self._waiter_counts[request_id] = remaining
            else:
                self._waiter_counts.pop(request_id, None)
                if record.status == "pending" and self._waiters.get(request_id) is waiter:
                    self._waiters.pop(request_id, None)

    def resolve(
        self,
        request_id: str,
        response: ApprovalResponseKind,
        feedback: str = "",
        *,
        approved_via_session_cache: bool = False,
    ) -> ApprovalRequestRecord | None:
        """Resolve a pending request (idempotent; returns the record)."""
        record = self._requests.get(request_id)
        if record is None or record.status != "pending":
            return record
        record.status = "resolved"
        record.response = response
        record.feedback = feedback
        record.approved_via_session_cache = approved_via_session_cache
        record.resolved_at = time.time()
        waiter = self._waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result((response, feedback))
        self._publish(ApprovalEvent(kind="request_resolved", request=record))
        self._publish_wire_response(request_id, response, feedback)
        return record

    def cancel(self, request_id: str, feedback: str = "") -> int:
        """Cancel one request; returns 1 when something was cancelled."""
        record = self._requests.get(request_id)
        if record is None or record.status != "pending":
            return 0
        record.status = "cancelled"
        record.response = "reject"
        record.feedback = feedback
        record.resolved_at = time.time()
        waiter = self._waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(ApprovalCancelledError(request_id))
        self._publish(ApprovalEvent(kind="request_resolved", request=record))
        self._publish_wire_response(request_id, "reject", feedback)
        return 1

    def cancel_by_source(self, kind: ApprovalSourceKind, source_id: str) -> int:
        """Cancel all pending requests from one source; returns the count."""
        cancelled = 0
        for record in list(self._requests.values()):
            if (
                record.status == "pending"
                and record.source.kind == kind
                and record.source.id == source_id
            ):
                cancelled += self.cancel(record.id, feedback="source cancelled")
        return cancelled

    # -- inspection -----------------------------------------------------
    def list_pending(self) -> list[ApprovalRequestRecord]:
        """All requests still awaiting a decision."""
        return [r for r in self._requests.values() if r.status == "pending"]

    def get_request(self, request_id: str) -> ApprovalRequestRecord | None:
        """Fetch a request by id (any status)."""
        return self._requests.get(request_id)

    # -- events ---------------------------------------------------------
    def subscribe(self, callback: Callable[[ApprovalEvent], None]) -> str:
        """Subscribe to request lifecycle events; returns an unsubscribe token."""
        token = f"sub_{uuid.uuid4().hex[:8]}"
        self._subscribers[token] = callback
        return token

    def unsubscribe(self, token: str) -> None:
        """Remove a subscription (never raises)."""
        self._subscribers.pop(token, None)

    def _publish(self, event: ApprovalEvent) -> None:
        for callback in list(self._subscribers.values()):
            try:
                callback(event)
            except Exception:
                continue

    def _publish_wire_request(self, record: ApprovalRequestRecord) -> None:
        if self._root_wire_hub is None:
            return
        try:
            from coderai.wire.types import ApprovalRequest

            self._root_wire_hub.publish_nowait(
                ApprovalRequest(
                    id=record.id,
                    tool_call_id=record.tool_call_id,
                    sender=record.sender,
                    action=record.action,
                    description=record.description,
                    source_kind=record.source.kind,
                    source_id=record.source.id,
                    agent_id=record.source.agent_id,
                    subagent_type=record.source.subagent_type,
                    display=list(record.display),
                )
            )
        except Exception:
            pass

    def _publish_wire_response(
        self, request_id: str, response: ApprovalResponseKind, feedback: str = ""
    ) -> None:
        if self._root_wire_hub is None:
            return
        try:
            from coderai.wire.types import ApprovalResponse

            self._root_wire_hub.publish_nowait(
                ApprovalResponse(request_id=request_id, response=response, feedback=feedback)
            )
        except Exception:
            pass

    def to_public_dict(self, record: ApprovalRequestRecord) -> dict[str, Any]:
        """JSON-safe view for UI / wire adapters."""
        return {
            "id": record.id,
            "toolCallId": record.tool_call_id,
            "sender": record.sender,
            "action": record.action,
            "description": record.description,
            "display": list(record.display),
            "status": record.status,
            "response": record.response,
            "feedback": record.feedback,
            "approvedViaSessionCache": record.approved_via_session_cache,
            "createdAt": record.created_at,
            "resolvedAt": record.resolved_at,
            "source": {
                "kind": record.source.kind,
                "id": record.source.id,
                "agentId": record.source.agent_id,
                "subagentType": record.source.subagent_type,
            },
        }
