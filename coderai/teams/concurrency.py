"""Optimistic concurrency control (CAS) revision errors."""

from __future__ import annotations


class ConcurrencyConflictError(ValueError):
    """Raised when an optimistic concurrency check (CAS revision mismatch) fails."""

    def __init__(
        self,
        message: str,
        resource_id: str | None = None,
        expected_revision: int | None = None,
        actual_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.resource_id = resource_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
