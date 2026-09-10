# Ported from coderai/core/denwarenji.py - kimi structure (soul/denwarenji.py).
"""Checkpoint-scoped D-Mail steering (Kimi ``soul/denwarenji.py`` parity).

A D-Mail carries ``(message, checkpoint_id)``: the directive is injected at
the checkpoint with that id so the agent re-steers from a known-good point.
Only one D-Mail may be pending at a time; checkpoint ids are validated
against the soul-owned checkpoint count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coderai.exception import CoderAIException


class DenwaRenjiError(CoderAIException):
    """Raised when a D-Mail cannot be accepted."""


@dataclass(frozen=True, slots=True)
class DMail:
    message: str
    checkpoint_id: int


@dataclass(slots=True)
class DenwaRenji:
    _pending_dmail: DMail | None = field(default=None, init=False)
    _n_checkpoints: int = field(default=0, init=False)

    def send_dmail(self, dmail: DMail) -> None:
        """Stage a D-Mail for the next checkpoint restore.

        Raises:
            DenwaRenjiError: If one is already pending or the id is invalid.
        """
        if self._pending_dmail is not None:
            raise DenwaRenjiError("Only one D-Mail can be sent at a time")
        if dmail.checkpoint_id < 0:
            raise DenwaRenjiError("The checkpoint ID can not be negative")
        if dmail.checkpoint_id >= self._n_checkpoints:
            raise DenwaRenjiError("There is no checkpoint with the given ID")
        self._pending_dmail = dmail

    def set_n_checkpoints(self, n_checkpoints: int) -> None:
        """Record the current checkpoint count (called by the soul)."""
        self._n_checkpoints = max(0, int(n_checkpoints))

    @property
    def n_checkpoints(self) -> int:
        return self._n_checkpoints

    def fetch_pending_dmail(self) -> DMail | None:
        """Take the pending D-Mail, clearing the slot."""
        pending = self._pending_dmail
        self._pending_dmail = None
        return pending
