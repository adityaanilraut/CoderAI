"""Undo / rollback tool for reverting file changes."""

import asyncio
import json
import logging
import os
import shutil
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, cast, runtime_checkable

from pydantic import BaseModel, Field

from coderAI.system.fsperms import OWNER_RW, OWNER_RWX, atomic_write_json, restrict_path
from coderAI.tools.base import Tool

logger = logging.getLogger(__name__)

# Maximum number of backups to keep per file
MAX_BACKUPS_PER_FILE = 10

# Maximum total backups across all files
MAX_TOTAL_BACKUPS = 50


def _reapply_saved_mode(filepath: Path, entry: Dict[str, Any]) -> None:
    """Best-effort restore of the file's original permission bits after copy2.

    Backups are chmod'd to 0600 at rest, so ``shutil.copy2`` would otherwise
    stamp the restored file 0600 and drop bits such as the executable flag. Reapply
    the mode captured when the backup was taken. No-op on Windows and for legacy
    backups without a recorded mode.
    """
    mode = entry.get("mode")
    if not isinstance(mode, int) or os.name == "nt":
        return
    try:
        os.chmod(filepath, mode)
    except OSError:
        pass


@runtime_checkable
class BackupStoreProtocol(Protocol):
    """Protocol defining the backup store interface used by tools.

    Both ``FileBackupStore`` and ``_LazyBackupStore`` implement this
    interface, so the ``backup_store`` module-level variable can be
    statically typed without ``type: ignore``.
    """

    def backup_file(self, file_path: str, operation: str) -> Any: ...


class FileBackupStore:
    """Stores file backups for undo operations."""

    def __init__(self, backup_dir: Optional[str] = None):
        """Initialize backup store.

        Args:
            backup_dir: Directory for storing backups (default: ~/.coderAI/backups)
        """
        self._custom_backup_dir = backup_dir
        self._last_resolved_dir: Optional[Path] = None
        self._cached_index: List[Dict[str, Any]] = []

    @property
    def backup_dir(self) -> Path:
        if self._custom_backup_dir:
            d = Path(self._custom_backup_dir)
        else:
            from coderAI.system.history import history_manager

            base_dir = Path.home() / ".coderAI" / "backups"
            if history_manager.current_session:
                d = base_dir / history_manager.current_session.session_id
            else:
                d = base_dir / "global"
        d.mkdir(parents=True, exist_ok=True)
        # Backups are copies of project files (potentially secret-bearing) that
        # live under ~/.coderAI — keep the directory owner-only (0700).
        restrict_path(d, OWNER_RWX)
        return d

    @property
    def index_file(self) -> Path:
        return self.backup_dir / "index.json"

    @property
    def index(self) -> List[Dict[str, Any]]:
        current_dir = self.backup_dir
        if self._last_resolved_dir != current_dir:
            self._last_resolved_dir = current_dir
            self._cached_index = self._load_index()
        return self._cached_index

    @index.setter
    def index(self, val: List[Dict[str, Any]]):
        self._cached_index = val

    def _load_index(self) -> List[Dict[str, Any]]:
        """Load backup index from disk."""
        if self.index_file.exists():
            try:
                with open(self.index_file, "r") as f:
                    return cast(List[Dict[str, Any]], json.load(f))
            except Exception as e:
                logger.warning("Could not load backup index %s: %s", self.index_file, e)
                return []
        return []

    def _save_index(self):
        """Atomically write the backup index to disk.

        A non-atomic write could leave a truncated ``index.json`` that fails to
        parse on the next load, silently resetting the store to an empty index;
        :func:`atomic_write_json` writes-then-replaces (and ``fsync``s) to
        prevent that.
        """
        atomic_write_json(self.index_file, self.index, fsync=True)

    def backup_file(self, filepath: str, operation: str = "modify") -> Dict[str, Any]:
        """Create a backup of a file before modification.

        Args:
            filepath: Path to the file to backup
            operation: Type of operation (modify, delete, create)

        Returns:
            Backup info dict
        """
        source = Path(filepath).expanduser().resolve()

        if not source.exists():
            if operation == "create":
                # For new files, just record that they didn't exist before
                entry: Dict[str, Any] = {
                    "filepath": str(source),
                    "backup_path": None,
                    "operation": "create",
                    "timestamp": datetime.now().isoformat(),
                }
                self.index.append(entry)
                self._save_index()
                return entry
            return {"error": f"File not found: {filepath}"}

        # Create backup copy
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_name = source.name.replace("/", "_")
        backup_name = f"{safe_name}.{timestamp}.bak"
        backup_path = self.backup_dir / backup_name

        # Record the source's permission bits *before* we tighten the backup, so
        # a restore can re-apply the original mode (e.g. an executable bit).
        source_mode = stat.S_IMODE(source.stat().st_mode)
        shutil.copy2(source, backup_path)
        # copy2 preserves the source mode, which may be world-readable — restrict
        # the at-rest backup to owner-only (0600).
        restrict_path(backup_path, OWNER_RW)

        entry = {
            "filepath": str(source),
            "backup_path": str(backup_path),
            "operation": operation,
            "timestamp": datetime.now().isoformat(),
            "mode": source_mode,
        }
        self.index.append(entry)
        self._save_index()

        # Clean up old backups for this file and globally
        self._cleanup_old_backups(str(source))
        self._cleanup_global_backups()

        return entry

    def undo_last(self) -> Dict[str, Any]:
        """Undo the most recent file operation.

        Returns:
            Result of the undo operation
        """
        if not self.index:
            return {"success": False, "error": "No operations to undo"}

        entry = self.index.pop()
        self._save_index()

        filepath = Path(entry["filepath"])
        operation = entry["operation"]

        try:
            if operation == "create":
                # File was created — undo by deleting it
                if filepath.exists():
                    filepath.unlink()
                return {
                    "success": True,
                    "action": "deleted",
                    "filepath": str(filepath),
                    "message": f"Removed newly created file: {filepath.name}",
                }

            elif operation in ("modify", "delete"):
                # Restore from backup
                backup_path = Path(entry["backup_path"])
                if not backup_path.exists():
                    return {
                        "success": False,
                        "error": f"Backup file not found: {backup_path}",
                    }

                # Ensure parent directory exists (for deleted files)
                filepath.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, filepath)
                _reapply_saved_mode(filepath, entry)

                # Clean up the backup file
                backup_path.unlink()

                return {
                    "success": True,
                    "action": "restored",
                    "filepath": str(filepath),
                    "message": f"Restored {filepath.name} to previous version",
                }

            else:
                return {"success": False, "error": f"Unknown operation type: {operation}"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def undo_specific(self, index: int) -> Dict[str, Any]:
        """Undo a specific operation by its index in the history.

        Args:
            index: 0-based index into the history (0 = most recent)

        Returns:
            Result of the undo operation
        """
        if not self.index:
            return {"success": False, "error": "No operations to undo"}

        if index < 0 or index >= len(self.index):
            return {
                "success": False,
                "error": f"Invalid index {index}. Valid range: 0-{len(self.index) - 1}",
            }

        # Convert 0=most-recent to actual list index
        actual_idx = len(self.index) - 1 - index
        entry = self.index.pop(actual_idx)
        self._save_index()

        filepath = Path(entry["filepath"])
        operation = entry["operation"]

        try:
            if operation == "create":
                if filepath.exists():
                    filepath.unlink()
                return {
                    "success": True,
                    "action": "deleted",
                    "filepath": str(filepath),
                    "message": f"Removed newly created file: {filepath.name}",
                }
            elif operation in ("modify", "delete"):
                backup_path = Path(entry["backup_path"])
                if not backup_path.exists():
                    return {"success": False, "error": f"Backup file not found: {backup_path}"}
                filepath.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, filepath)
                _reapply_saved_mode(filepath, entry)
                backup_path.unlink()
                return {
                    "success": True,
                    "action": "restored",
                    "filepath": str(filepath),
                    "message": f"Restored {filepath.name} to version from {entry['timestamp']}",
                }
            else:
                return {"success": False, "error": f"Unknown operation type: {operation}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def restore_after(self, cutoff_epoch: float) -> Dict[str, Any]:
        """Revert every backup recorded after ``cutoff_epoch`` (newest first).

        Used by conversation rewind (``/rewind <turn> --files``) to undo file
        edits made since a checkpoint. Entries at or before the cutoff are left
        untouched. Consumed entries are dropped from the index, matching the
        single-step ``undo_last`` behaviour.

        Args:
            cutoff_epoch: A ``time.time()`` value; backups with a newer
                timestamp are reverted.

        Returns:
            ``{"success", "restored", "deleted", "errors", "count"}``.
        """

        def _epoch(entry: Dict[str, Any]) -> float:
            ts = entry.get("timestamp")
            if not isinstance(ts, str):
                return 0.0
            try:
                return datetime.fromisoformat(ts).timestamp()
            except ValueError:
                return 0.0

        to_undo = [e for e in self.index if _epoch(e) > cutoff_epoch]
        if not to_undo:
            return {"success": True, "restored": [], "deleted": [], "errors": [], "count": 0}

        restored: List[str] = []
        deleted: List[str] = []
        errors: List[str] = []

        # Newest first so layered edits to a single file unwind in order.
        for entry in sorted(to_undo, key=_epoch, reverse=True):
            filepath = Path(entry["filepath"])
            operation = entry.get("operation")
            try:
                if operation == "create":
                    if filepath.exists():
                        filepath.unlink()
                    deleted.append(str(filepath))
                elif operation in ("modify", "delete"):
                    backup_path_str = entry.get("backup_path")
                    if not backup_path_str or not Path(backup_path_str).exists():
                        errors.append(f"{filepath.name}: backup missing")
                        continue
                    filepath.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_path_str, filepath)
                    _reapply_saved_mode(filepath, entry)
                    Path(backup_path_str).unlink()
                    restored.append(str(filepath))
                else:
                    errors.append(f"{filepath.name}: unknown operation {operation!r}")
            except Exception as e:
                errors.append(f"{filepath.name}: {e}")

        # Drop the consumed entries (by identity) and persist the index.
        consumed = {id(e) for e in to_undo}
        self.index = [e for e in self.index if id(e) not in consumed]
        self._save_index()

        return {
            "success": True,
            "restored": restored,
            "deleted": deleted,
            "errors": errors,
            "count": len(restored) + len(deleted),
        }

    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent backup history.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of backup entries (most recent first)
        """
        return list(reversed(self.index[-limit:]))

    def _cleanup_old_backups(self, filepath: str):
        """Remove old backups beyond MAX_BACKUPS_PER_FILE."""
        file_entries = [e for e in self.index if e["filepath"] == filepath]
        if len(file_entries) > MAX_BACKUPS_PER_FILE:
            excess = file_entries[: len(file_entries) - MAX_BACKUPS_PER_FILE]
            to_remove = set(id(e) for e in excess)
            # Rebuild and save index first so a crash during file deletion
            # doesn't leave dangling index entries.
            self.index = [e for e in self.index if id(e) not in to_remove]
            self._save_index()
            for entry in excess:
                if entry.get("backup_path"):
                    backup = Path(entry["backup_path"])
                    if backup.exists():
                        try:
                            backup.unlink()
                        except OSError:
                            pass

    def _cleanup_global_backups(self):
        """Prune oldest backups when total exceeds MAX_TOTAL_BACKUPS."""
        while len(self.index) > MAX_TOTAL_BACKUPS:
            entry = self.index.pop(0)  # Remove oldest
            if entry.get("backup_path"):
                backup = Path(entry["backup_path"])
                if backup.exists():
                    backup.unlink()
        self._save_index()


def get_backup_store() -> FileBackupStore:
    """Resolve the active backup store (process-shared via ToolServices)."""
    from coderAI.core.services import get_services

    return get_services().backup_store


class _LazyBackupStore:
    """Module-level proxy that defers ``FileBackupStore`` creation until first use.

    Avoids creating ``~/.coderAI/backups/`` and touching ``index.json`` on
    import (e.g. ``coderAI --version``).
    """

    def __getattr__(self, name):
        return getattr(get_backup_store(), name)

    def __repr__(self):
        return repr(get_backup_store())


# Backward-compat alias — lazily delegates to the real store on first use
backup_store: BackupStoreProtocol = _LazyBackupStore()


class UndoParams(BaseModel):
    index: Optional[int] = Field(
        None,
        description="Index of the operation to undo (0 = most recent). Use undo_history to see available indices.",
    )


class UndoTool(Tool):
    """Tool for undoing file operations."""

    name = "undo"
    description = "Undo a file modification (restores previous version). Optionally specify an index from undo_history."
    category = "undo"
    parameters_model = UndoParams
    requires_confirmation = True

    async def execute(self, index: Optional[int] = None) -> Dict[str, Any]:  # type: ignore[override]
        """Undo a file operation."""
        store = get_backup_store()
        if index is not None:
            return await asyncio.to_thread(store.undo_specific, index)
        return await asyncio.to_thread(store.undo_last)


class UndoHistoryParams(BaseModel):
    limit: int = Field(10, description="Number of entries to show (default: 10)")


class UndoHistoryTool(Tool):
    """Tool for viewing file modification history."""

    name = "undo_history"
    description = "View recent file modification history for undo"
    category = "undo"
    parameters_model = UndoHistoryParams
    is_read_only = True

    async def execute(self, limit: int = 10) -> Dict[str, Any]:  # type: ignore[override]
        """Get undo history."""
        history = get_backup_store().get_history(limit)
        return {
            "success": True,
            "entries": history,
            "count": len(history),
        }
