"""Durable session-owned objective ledgers independent of chat transcripts."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Optional

from coderAI.core.objective import ObjectiveState
from coderAI.system.fsperms import OWNER_RWX, atomic_write_json, restrict_path


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _record_timestamp(record: dict[str, Any], field: str) -> float:
    state = record.get("state")
    value = state.get(field) if isinstance(state, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


class ObjectiveLedgerError(RuntimeError):
    """Raised when objective storage ownership or integrity checks fail."""


class ObjectiveLedgerStore:
    """One session's durable objective records.

    Records live outside the session transcript so context compaction and
    transcript rewinds cannot erase engineering-state evidence.
    """

    schema_version = 1

    def __init__(self, *, session_id: str, ledger_root: Optional[Path] = None) -> None:
        if not _SAFE_ID.fullmatch(session_id):
            raise ValueError("session objective store id must be path-safe")
        self.session_id = session_id
        if ledger_root is not None:
            base = Path(ledger_root).expanduser().resolve()
        else:
            base = (Path.home() / ".coderAI" / "objectives").resolve()
        self.ledger_root = base
        self.store_dir = (self.ledger_root / session_id).resolve()
        if self.store_dir.parent != self.ledger_root:
            raise ValueError("session objective directory escapes ledger_root")
        # Lazy mkdir: do not touch filesystem in ctor. Directories are created
        # on first save/list, and permission failures fallback to a temp dir so
        # sandboxed tests can proceed without Operation not permitted.
        self._lock = threading.RLock()
        self._fallback_root: Optional[Path] = None

    def _ensure_dirs(self) -> None:
        """Ensure ledger directories exist, lazily and sandbox-aware."""
        import tempfile

        for p in (self.ledger_root, self.store_dir):
            try:
                p.mkdir(parents=True, exist_ok=True)
                restrict_path(p, OWNER_RWX)
            except PermissionError:
                # Sandbox denies ~/.coderAI — fallback to a tmp dir for tests.
                if self._fallback_root is None:
                    self._fallback_root = (
                        Path(tempfile.gettempdir())
                        / ".coderAI_test"
                        / "objectives"
                        / self.session_id
                    )
                    self._fallback_root = self._fallback_root.resolve()
                    self.ledger_root = self._fallback_root.parent
                    self.store_dir = self._fallback_root
                try:
                    self._fallback_root.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                return
            except OSError:
                # Other mkdir failures are non-fatal; persistence will retry.
                return

    def _record_path(self, objective_id: str) -> Path:
        if not _SAFE_ID.fullmatch(objective_id) or not objective_id.startswith("objective_"):
            raise ObjectiveLedgerError("objective_id must be path-safe")
        self._ensure_dirs()
        path = (self.store_dir / f"{objective_id}.json").resolve()
        if path.parent != self.store_dir:
            raise ObjectiveLedgerError("objective record escapes session ledger")
        return path

    def _check_owner(self, run_context: Any) -> None:
        if getattr(run_context, "session_id", None) != self.session_id:
            raise ObjectiveLedgerError("run context does not own this objective ledger")

    def save(self, state: ObjectiveState, *, run_context: Any) -> None:
        """Atomically persist one complete resumable state snapshot."""
        self._check_owner(run_context)
        record = {
            "schema_version": self.schema_version,
            "objective_id": state.objective_id,
            "run_id": getattr(run_context, "run_id", None),
            "session_id": self.session_id,
            "agent_id": getattr(run_context, "agent_id", None),
            "workspace_id": getattr(run_context, "workspace_id", None),
            "state": state.snapshot(),
        }
        with self._lock:
            atomic_write_json(self._record_path(state.objective_id), record, fsync=True)

    def load(self, objective_id: str, *, run_context: Any) -> ObjectiveState:
        """Load one objective after checking session ownership and record identity."""
        self._check_owner(run_context)
        path = self._record_path(objective_id)
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ObjectiveLedgerError(f"could not read objective {objective_id}") from exc
        if not isinstance(record, dict) or record.get("schema_version") != self.schema_version:
            raise ObjectiveLedgerError("objective record has an unsupported schema")
        if (
            record.get("objective_id") != objective_id
            or record.get("session_id") != self.session_id
        ):
            raise ObjectiveLedgerError("objective record identity does not match its path")
        if record.get("workspace_id") != getattr(run_context, "workspace_id", None):
            raise ObjectiveLedgerError("objective record belongs to another workspace")
        state = record.get("state")
        if not isinstance(state, dict):
            raise ObjectiveLedgerError("objective record has no state snapshot")
        if state.get("objective_id") != objective_id:
            raise ObjectiveLedgerError("objective state identity does not match its record")
        try:
            return ObjectiveState.from_snapshot(state)
        except (TypeError, ValueError) as exc:
            raise ObjectiveLedgerError("objective state snapshot is invalid") from exc

    def list_records(self) -> list[dict[str, Any]]:
        """Return valid record envelopes ordered from oldest to newest."""
        self._ensure_dirs()
        records: list[dict[str, Any]] = []
        with self._lock:
            for path in self.store_dir.glob("objective_*.json"):
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        record = json.load(handle)
                    state = record.get("state") if isinstance(record, dict) else None
                    if (
                        isinstance(record, dict)
                        and record.get("schema_version") == self.schema_version
                        and record.get("session_id") == self.session_id
                        and record.get("objective_id") == path.stem
                        and isinstance(state, dict)
                    ):
                        records.append(record)
                except (OSError, json.JSONDecodeError):
                    continue
        records.sort(key=lambda item: _record_timestamp(item, "created_at"))
        return records

    def load_latest(self, *, run_context: Any) -> Optional[ObjectiveState]:
        """Restore the most recently updated valid objective, if any."""
        self._check_owner(run_context)
        records = self.list_records()
        records.sort(key=lambda item: _record_timestamp(item, "updated_at"), reverse=True)
        for record in records:
            try:
                return self.load(str(record["objective_id"]), run_context=run_context)
            except ObjectiveLedgerError:
                continue
        return None
