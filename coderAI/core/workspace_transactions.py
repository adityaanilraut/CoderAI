"""Durable, session-owned workspace transaction ledger.

Mutating tool calls are bracketed by a pre-execution workspace snapshot and a
post-execution observation.  The ledger lives outside the project tree, so it
can account for native file tools, shell commands, and tool hooks without
changing the filesystem permission boundary those operations already use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from coderAI.system.fsperms import OWNER_RW, OWNER_RWX, atomic_write_json, restrict_path


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_EXCLUDED_TOP_LEVEL = frozenset({".git"})


class TransactionState(str, Enum):
    """Persisted workspace transaction states."""

    OPEN = "open"
    RECORDED = "recorded"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    PARTIAL_FAILURE = "partially_failed"
    RECOVERED = "recovered"


ROLLBACKABLE_STATES = frozenset(
    {
        TransactionState.COMMITTED.value,
        TransactionState.RECOVERED.value,
        TransactionState.PARTIAL_FAILURE.value,
    }
)


class WorkspaceTransactionError(RuntimeError):
    """Raised when a mutation cannot be safely represented by the ledger."""


@dataclass(frozen=True)
class WorkspaceTransactionHandle:
    transaction_id: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "rb", closefd=False) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _copy_snapshot_file(source: Path, destination: Path, mode: int) -> str:
    """Copy a regular file without following a swapped symlink leaf."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    restrict_path(destination.parent, OWNER_RWX)
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    try:
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_RW)
        try:
            digest = hashlib.sha256()
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    restrict_path(destination, OWNER_RW)
    # ``mode`` is retained in the manifest for restore; snapshots stay 0600.
    del mode
    return digest.hexdigest()


class WorkspaceTransactionStore:
    """A durable transaction ledger owned by one session and workspace."""

    def __init__(
        self,
        *,
        session_id: str,
        workspace_root: str,
        ledger_root: Optional[str] = None,
    ) -> None:
        if not _SAFE_ID.fullmatch(session_id):
            raise ValueError("session_id must be a non-empty path-safe identifier")
        workspace = Path(workspace_root).expanduser().resolve()
        root = (
            (
                Path(ledger_root)
                if ledger_root is not None
                else Path.home() / ".coderAI" / "transactions"
            )
            .expanduser()
            .resolve()
        )
        store_dir = (root / session_id).resolve()
        if not store_dir.is_relative_to(root):
            raise ValueError("session transaction directory escapes ledger_root")
        self.session_id = session_id
        self.workspace_root = workspace
        self.workspace_id = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
        self._ledger_root = root
        self._store_dir = store_dir
        self._lock = threading.RLock()

    @property
    def store_dir(self) -> Path:
        self._store_dir.mkdir(parents=True, exist_ok=True)
        restrict_path(self._ledger_root, OWNER_RWX)
        restrict_path(self._store_dir, OWNER_RWX)
        return self._store_dir

    def _validate_context(self, run_context: Any) -> None:
        if getattr(run_context, "session_id", None) != self.session_id:
            raise WorkspaceTransactionError("run context does not own this session ledger")
        if getattr(run_context, "workspace_id", None) != self.workspace_id:
            raise WorkspaceTransactionError("run context does not own this workspace ledger")
        context_root = getattr(run_context, "workspace_root", None)
        if context_root is None or Path(context_root).expanduser().resolve() != self.workspace_root:
            raise WorkspaceTransactionError("run context workspace root does not match ledger")

    def _transaction_dir(self, transaction_id: str) -> Path:
        if not _SAFE_ID.fullmatch(transaction_id):
            raise WorkspaceTransactionError("transaction_id must be path-safe")
        path = (self.store_dir / transaction_id).resolve()
        if not path.is_relative_to(self.store_dir):
            raise WorkspaceTransactionError("transaction directory escapes session ledger")
        return path

    def _excluded(self, path: Path, relative: Path) -> bool:
        if relative.parts and relative.parts[0] in _EXCLUDED_TOP_LEVEL:
            return True
        try:
            path.resolve().is_relative_to(self.store_dir)
        except (OSError, RuntimeError):
            return False
        return path.resolve().is_relative_to(self.store_dir)

    def _scan_workspace(
        self, *, backup_dir: Optional[Path] = None
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        states: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        if not self.workspace_root.is_dir():
            return states, [f"workspace root is unavailable: {self.workspace_root}"]

        def visit(directory: Path) -> None:
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                errors.append(f"{directory}: {exc}")
                return
            for entry in entries:
                path = Path(entry.path)
                try:
                    relative = path.relative_to(self.workspace_root)
                except ValueError:
                    errors.append(f"path escaped workspace during scan: {path}")
                    continue
                if self._excluded(path, relative):
                    continue
                key = relative.as_posix()
                try:
                    info = path.lstat()
                    mode = stat.S_IMODE(info.st_mode)
                    if stat.S_ISLNK(info.st_mode):
                        states[key] = {
                            "kind": "symlink",
                            "mode": mode,
                            "target": os.readlink(path),
                        }
                    elif stat.S_ISDIR(info.st_mode):
                        states[key] = {"kind": "directory", "mode": mode}
                        visit(path)
                    elif stat.S_ISREG(info.st_mode):
                        if backup_dir is None:
                            digest = _sha256_file(path)
                        else:
                            digest = _copy_snapshot_file(path, backup_dir / relative, mode)
                        states[key] = {
                            "kind": "file",
                            "mode": mode,
                            "size": info.st_size,
                            "sha256": digest,
                        }
                    else:
                        states[key] = {"kind": "other", "mode": mode}
                except (OSError, RuntimeError) as exc:
                    errors.append(f"{key}: {exc}")

        visit(self.workspace_root)
        return states, errors

    @staticmethod
    def _changes(
        before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for path in sorted(set(before) | set(after)):
            old = before.get(path)
            new = after.get(path)
            if old == new:
                continue
            operation = "created" if old is None else "deleted" if new is None else "modified"
            changes.append({"path": path, "operation": operation, "before": old, "after": new})
        return changes

    @staticmethod
    def _transition(data: dict[str, Any], state: TransactionState, reason: str) -> None:
        timestamp = _now()
        data["state"] = state.value
        data["updated_at"] = timestamp
        data.setdefault("transitions", []).append(
            {"state": state.value, "at": timestamp, "reason": reason}
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        if not isinstance(data, dict):
            raise WorkspaceTransactionError(f"transaction record must be an object: {path}")
        return data

    def _write_record(self, transaction_dir: Path, data: dict[str, Any]) -> None:
        atomic_write_json(transaction_dir / "transaction.json", data, fsync=True)

    def begin(
        self,
        *,
        run_context: Any,
        tool_call_id: str,
        tool_name: str,
        tool_arguments: Any,
        objective: Optional[str],
        plan_id: Optional[str],
        plan_revision: Optional[int],
    ) -> WorkspaceTransactionHandle:
        """Persist a complete pre-mutation snapshot and enter ``open``."""
        self._validate_context(run_context)
        transaction_id = f"txn_{uuid.uuid4().hex}"
        transaction_dir = self._transaction_dir(transaction_id)
        with self._lock:
            transaction_dir.mkdir(mode=OWNER_RWX)
            restrict_path(transaction_dir, OWNER_RWX)
            before_dir = transaction_dir / "before"
            before_dir.mkdir(mode=OWNER_RWX)
            restrict_path(before_dir, OWNER_RWX)
            try:
                before, errors = self._scan_workspace(backup_dir=before_dir)
            except Exception as exc:
                before, errors = {}, [str(exc)]

            policy = getattr(run_context, "permission_policy", None)
            allowed_tools = sorted(getattr(policy, "allowed_tools", ()) or ())
            arguments_json = json.dumps(tool_arguments, sort_keys=True, default=str)
            created_at = _now()
            data: dict[str, Any] = {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "session_id": self.session_id,
                "run_id": getattr(run_context, "run_id", None),
                "agent_id": getattr(run_context, "agent_id", None),
                "workspace_id": self.workspace_id,
                "workspace_root": str(self.workspace_root),
                "isolation_domain": getattr(run_context, "isolation_domain", None),
                "permission_policy": {
                    "auto_approve": bool(getattr(policy, "auto_approve", False)),
                    "workspace_trusted": bool(getattr(policy, "workspace_trusted", False)),
                    "allowed_tools": allowed_tools,
                },
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "tool_arguments_sha256": hashlib.sha256(arguments_json.encode("utf-8")).hexdigest(),
                "objective": objective,
                "plan_execution": (
                    {"plan_id": plan_id, "revision": plan_revision}
                    if plan_id and plan_revision
                    else None
                ),
                "created_at": created_at,
                "created_at_epoch": time.time(),
                "updated_at": created_at,
                "state": TransactionState.OPEN.value,
                "transitions": [],
                "changes": [],
                "errors": list(errors),
                "rollback_ready": False,
            }
            atomic_write_json(transaction_dir / "before.json", before, fsync=True)
            if errors:
                data["rollback_ready"] = False
                self._transition(
                    data,
                    TransactionState.PARTIAL_FAILURE,
                    "pre-mutation snapshot was incomplete; tool execution rejected",
                )
                self._write_record(transaction_dir, data)
                raise WorkspaceTransactionError(
                    f"workspace transaction {transaction_id} could not open: {errors[0]}"
                )
            self._transition(data, TransactionState.OPEN, "pre-mutation snapshot persisted")
            self._write_record(transaction_dir, data)
        return WorkspaceTransactionHandle(transaction_id)

    def finalize(
        self,
        handle: WorkspaceTransactionHandle,
        *,
        run_context: Any,
        tool_result: Any,
    ) -> dict[str, Any]:
        """Observe the workspace and durably commit the transaction outcome."""
        self._validate_context(run_context)
        transaction_dir = self._transaction_dir(handle.transaction_id)
        with self._lock:
            data = self._read_json(transaction_dir / "transaction.json")
            if data.get("state") != TransactionState.OPEN.value:
                raise WorkspaceTransactionError(f"transaction {handle.transaction_id} is not open")
            before = self._read_json(transaction_dir / "before.json")
            try:
                after, errors = self._scan_workspace()
            except Exception as exc:
                after, errors = {}, [str(exc)]
            data["changes"] = self._changes(before, after)
            success = isinstance(tool_result, dict) and tool_result.get("success") is True
            data["tool_success"] = success
            if isinstance(tool_result, dict) and tool_result.get("error"):
                data["tool_error"] = str(tool_result["error"])[:1000]
            if errors:
                data.setdefault("errors", []).extend(errors)
                self._transition(
                    data,
                    TransactionState.PARTIAL_FAILURE,
                    "post-mutation workspace observation was incomplete",
                )
            else:
                data["rollback_ready"] = True
                self._transition(
                    data,
                    TransactionState.RECORDED,
                    "post-mutation workspace changes recorded",
                )
                self._write_record(transaction_dir, data)
                self._transition(
                    data,
                    TransactionState.COMMITTED,
                    "transaction metadata committed",
                )
            self._write_record(transaction_dir, data)
            return data

    def recover_incomplete(self, *, run_context: Any) -> list[str]:
        """Recover durable metadata for transactions interrupted by a crash."""
        self._validate_context(run_context)
        recovered: list[str] = []
        with self._lock:
            for data in self.list_transactions():
                transaction_id = str(data.get("transaction_id") or "")
                state = data.get("state")
                if state not in {
                    TransactionState.OPEN.value,
                    TransactionState.RECORDED.value,
                    TransactionState.ROLLING_BACK.value,
                }:
                    continue
                transaction_dir = self._transaction_dir(transaction_id)
                if state == TransactionState.OPEN.value:
                    before = self._read_json(transaction_dir / "before.json")
                    after, errors = self._scan_workspace()
                    data["changes"] = self._changes(before, after)
                    data["tool_success"] = None
                    data.setdefault("errors", []).extend(errors)
                    data["rollback_ready"] = not errors
                    target = (
                        TransactionState.PARTIAL_FAILURE if errors else TransactionState.RECOVERED
                    )
                    reason = "open transaction observed during session resume"
                elif state == TransactionState.RECORDED.value:
                    target = TransactionState.RECOVERED
                    reason = "recorded transaction commit completed during session resume"
                else:
                    target = TransactionState.PARTIAL_FAILURE
                    reason = "interrupted rollback requires an explicit retry"
                data["recovered_by_run_id"] = getattr(run_context, "run_id", None)
                self._transition(data, target, reason)
                self._write_record(transaction_dir, data)
                recovered.append(transaction_id)
        return recovered

    def list_transactions(self, *, limit: Optional[int] = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.store_dir.exists():
            return records
        for path in self.store_dir.glob("txn_*/transaction.json"):
            try:
                records.append(self._read_json(path))
            except (OSError, ValueError, WorkspaceTransactionError):
                continue
        records.sort(key=lambda item: float(item.get("created_at_epoch", 0.0)), reverse=True)
        return records[:limit] if limit is not None else records

    def has_transactions(self) -> bool:
        return bool(self.list_transactions(limit=1))

    def latest_rollbackable(self) -> Optional[str]:
        for item in self.list_transactions():
            if (
                item.get("state") in ROLLBACKABLE_STATES
                and item.get("rollback_ready") is True
                and item.get("changes")
            ):
                return str(item["transaction_id"])
        return None

    def _path_for_change(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkspaceTransactionError(f"unsafe transaction path: {relative_path}")
        path = self.workspace_root / relative
        resolved_parent = path.parent.resolve()
        if not resolved_parent.is_relative_to(self.workspace_root):
            raise WorkspaceTransactionError(f"transaction path escapes workspace: {relative_path}")
        return path

    def _guard_path(self, path: Path, operation: str) -> Optional[str]:
        from coderAI.tools.filesystem._guards import (
            _enforce_project_scope,
            _is_path_protected,
            _reject_symlink_leaf,
        )

        if _is_path_protected(path):
            return f"Cannot {operation} protected path: {path}"
        scope_error = _enforce_project_scope(path, operation)
        if scope_error:
            return str(scope_error.get("error") or scope_error)
        if path.exists() or path.is_symlink():
            symlink_error = _reject_symlink_leaf(path, operation)
            if symlink_error:
                return str(symlink_error.get("error") or symlink_error)
        return None

    def _restore_file(self, backup: Path, target: Path, mode: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".transaction"
        )
        os.close(fd)
        temporary_path = Path(temporary)
        try:
            shutil.copyfile(backup, temporary_path, follow_symlinks=False)
            if os.name != "nt":
                os.chmod(temporary_path, mode)
            with open(temporary_path, "r+b") as restored:
                os.fsync(restored.fileno())
            os.replace(temporary_path, target)
        except Exception:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise

    def rollback(self, transaction_id: str, *, run_context: Any) -> dict[str, Any]:
        """Rollback one transaction, refusing to overwrite later changes."""
        self._validate_context(run_context)
        transaction_dir = self._transaction_dir(transaction_id)
        with self._lock:
            data = self._read_json(transaction_dir / "transaction.json")
            if (
                data.get("state") not in ROLLBACKABLE_STATES
                or data.get("rollback_ready") is not True
            ):
                return {
                    "success": False,
                    "transaction_id": transaction_id,
                    "error": f"Transaction is not rollbackable from state {data.get('state')!r}",
                }
            self._transition(data, TransactionState.ROLLING_BACK, "explicit rollback started")
            data["rollback_run_id"] = getattr(run_context, "run_id", None)
            data["rollback_agent_id"] = getattr(run_context, "agent_id", None)
            self._write_record(transaction_dir, data)

            current, scan_errors = self._scan_workspace()
            errors = list(scan_errors)
            restored: list[str] = []
            deleted: list[str] = []
            if scan_errors:
                data["rollback_errors"] = errors
                self._transition(
                    data,
                    TransactionState.PARTIAL_FAILURE,
                    "rollback refused because the current workspace scan was incomplete",
                )
                self._write_record(transaction_dir, data)
                return {
                    "success": False,
                    "transaction_id": transaction_id,
                    "state": data["state"],
                    "restored": restored,
                    "deleted": deleted,
                    "errors": errors,
                    "count": 0,
                }
            changes = list(data.get("changes") or [])
            invalid_paths: set[str] = set()
            for change in changes:
                relative_path = str(change.get("path") or "")
                try:
                    self._path_for_change(relative_path)
                except WorkspaceTransactionError as exc:
                    invalid_paths.add(relative_path)
                    errors.append(str(exc))

            def ready(change: dict[str, Any]) -> tuple[bool, bool]:
                path = str(change.get("path") or "")
                if path in invalid_paths:
                    return False, False
                present = current.get(path)
                before = change.get("before")
                after = change.get("after")
                if present == before:
                    return False, True
                if present != after:
                    errors.append(
                        f"{path}: workspace changed after transaction; refusing overwrite"
                    )
                    return False, False
                return True, False

            # Remove files created by the transaction before their directories.
            for change in changes:
                if change.get("operation") != "created":
                    continue
                after = change.get("after") or {}
                if after.get("kind") == "directory":
                    continue
                do_apply, already = ready(change)
                if not do_apply:
                    continue
                path = self._path_for_change(str(change["path"]))
                guard_error = self._guard_path(path, "rollback")
                if guard_error:
                    errors.append(f"{change['path']}: {guard_error}")
                    continue
                try:
                    path.unlink()
                    deleted.append(str(path))
                except OSError as exc:
                    errors.append(f"{change['path']}: {exc}")
                del already

            created_directories = sorted(
                (
                    change
                    for change in changes
                    if change.get("operation") == "created"
                    and (change.get("after") or {}).get("kind") == "directory"
                ),
                key=lambda item: len(Path(str(item["path"])).parts),
                reverse=True,
            )
            for change in created_directories:
                do_apply, _already = ready(change)
                if not do_apply:
                    continue
                path = self._path_for_change(str(change["path"]))
                guard_error = self._guard_path(path, "rollback")
                if guard_error:
                    errors.append(f"{change['path']}: {guard_error}")
                    continue
                try:
                    path.rmdir()
                    deleted.append(str(path))
                except OSError as exc:
                    errors.append(f"{change['path']}: {exc}")

            deleted_directories = sorted(
                (
                    change
                    for change in changes
                    if change.get("operation") == "deleted"
                    and (change.get("before") or {}).get("kind") == "directory"
                ),
                key=lambda item: len(Path(str(item["path"])).parts),
            )
            for change in deleted_directories:
                do_apply, _already = ready(change)
                if not do_apply:
                    continue
                path = self._path_for_change(str(change["path"]))
                guard_error = self._guard_path(path, "rollback")
                if guard_error:
                    errors.append(f"{change['path']}: {guard_error}")
                    continue
                try:
                    path.mkdir(exist_ok=False)
                    before = change.get("before") or {}
                    if os.name != "nt":
                        os.chmod(path, int(before.get("mode", 0o755)))
                    restored.append(str(path))
                except OSError as exc:
                    errors.append(f"{change['path']}: {exc}")

            for change in changes:
                if change.get("operation") == "created":
                    continue
                before = change.get("before") or {}
                if before.get("kind") == "directory":
                    if change.get("operation") == "modified":
                        do_apply, _already = ready(change)
                        if do_apply:
                            path = self._path_for_change(str(change["path"]))
                            guard_error = self._guard_path(path, "rollback")
                            if guard_error:
                                errors.append(f"{change['path']}: {guard_error}")
                            else:
                                try:
                                    if os.name != "nt":
                                        os.chmod(path, int(before.get("mode", 0o755)))
                                    restored.append(str(path))
                                except OSError as exc:
                                    errors.append(f"{change['path']}: {exc}")
                    continue
                do_apply, _already = ready(change)
                if not do_apply:
                    continue
                if before.get("kind") != "file":
                    errors.append(
                        f"{change['path']}: rollback of {before.get('kind', 'unknown')} entries is refused"
                    )
                    continue
                path = self._path_for_change(str(change["path"]))
                guard_error = self._guard_path(path, "rollback")
                if guard_error:
                    errors.append(f"{change['path']}: {guard_error}")
                    continue
                backup = transaction_dir / "before" / Path(str(change["path"]))
                if not backup.is_file():
                    errors.append(f"{change['path']}: pre-transaction backup is missing")
                    continue
                try:
                    self._restore_file(backup, path, int(before.get("mode", 0o600)))
                    restored.append(str(path))
                except OSError as exc:
                    errors.append(f"{change['path']}: {exc}")

            data["rollback_errors"] = errors
            data["rolled_back_at"] = _now()
            target_state = (
                TransactionState.PARTIAL_FAILURE if errors else TransactionState.ROLLED_BACK
            )
            self._transition(
                data,
                target_state,
                "rollback completed with errors" if errors else "rollback completed",
            )
            self._write_record(transaction_dir, data)
            return {
                "success": not errors,
                "transaction_id": transaction_id,
                "state": data["state"],
                "restored": restored,
                "deleted": deleted,
                "errors": errors,
                "count": len(restored) + len(deleted),
            }

    def rollback_after(self, cutoff_epoch: float, *, run_context: Any) -> dict[str, Any]:
        """Rollback rollbackable transactions after a checkpoint, newest first."""
        self._validate_context(run_context)
        restored: list[str] = []
        deleted: list[str] = []
        errors: list[str] = []
        transaction_ids: list[str] = []
        candidates = [
            item
            for item in self.list_transactions()
            if float(item.get("created_at_epoch", 0.0)) > cutoff_epoch
            and item.get("state") in ROLLBACKABLE_STATES
            and item.get("rollback_ready") is True
        ]
        for item in candidates:
            transaction_id = str(item["transaction_id"])
            result = self.rollback(transaction_id, run_context=run_context)
            transaction_ids.append(transaction_id)
            restored.extend(result.get("restored", []))
            deleted.extend(result.get("deleted", []))
            errors.extend(result.get("errors", []))
        return {
            "success": not errors,
            "transactions": transaction_ids,
            "restored": restored,
            "deleted": deleted,
            "errors": errors,
            "count": len(restored) + len(deleted),
        }
