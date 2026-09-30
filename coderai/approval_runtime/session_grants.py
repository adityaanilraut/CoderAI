"""Ephemeral session grants for repeated, identical ordinary tool actions.

Invocation IDs may change, but the owner, workspace, tool, normalized argument
digest and complete permission scopes must match. Elevated sandbox and hook
approvals always require a new explicit approval bound to the invocation ID.
"""

from __future__ import annotations

from typing import Any

EXPLICIT_APPROVAL_SCOPES = frozenset({"sandbox-escalation", "hook-approval"})


class SessionApprovalStore:
    def __init__(self) -> None:
        self._grants: set[tuple[Any, ...]] = set()

    @staticmethod
    def _key(request: dict[str, Any], scopes: list[str]) -> tuple[Any, ...] | None:
        if request.get("requiresExplicitApproval") or EXPLICIT_APPROVAL_SCOPES.intersection(scopes):
            return None
        binding = tuple(
            request.get(field)
            for field in ("session_id", "project_root", "tool_name", "args_digest")
        )
        if not scopes or not all(isinstance(value, str) and value for value in binding):
            return None
        return (*binding, tuple(sorted(set(scopes))))

    def grant(self, request: dict[str, Any]) -> bool:
        scopes = request.get("all_scopes", request.get("scopes")) or []
        key = self._key(request, scopes)
        if key is None:
            return False
        self._grants.add(key)
        return True

    def matches(self, binding: dict[str, Any], scopes: list[str]) -> bool:
        key = self._key(binding, scopes)
        return key is not None and key in self._grants

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._grants.clear()
        else:
            self._grants = {key for key in self._grants if key[0] != session_id}
