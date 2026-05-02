"""Undo / rollback tool for reverting file changes."""

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

# Maximum number of backups to keep per file
MAX_BACKUPS_PER_FILE = 10

# Maximum total backups across all files
MAX_TOTAL_BACKUPS = 50


class FileBackupStore:
    """Stores file backups for undo operations."""

    def __init__(self, backup_dir: str = None):
        """Initialize backup store.

        Args:
            backup_dir: Directory for storing backups (default: ~/.coderAI/backups)
        """
        if backup_dir:
            self.backup_dir = Path(backup_dir)
        else:
            self.backup_dir = Path.home() / ".coderAI" / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        # Index file tracking all backups
        self.index_file = self.backup_dir / "index.json"
        self.index: List[Dict[str, Any]] = self._load_index()

    def _load_index(self) -> List[Dict[str, Any]]:
        """Load backup index from disk."""
        if self.index_file.exists():
            try:
                with open(self.index_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, Exception):
                return []
        return []

    def _save_index(self):
        """Atomically write the backup index to disk.

        A crash between ``open()`` and ``close()`` would otherwise leave a
        truncated ``index.json`` that fails to parse on the next load, and
        the store would silently reset to an empty index. Write to a
        temp file in the same directory, then ``os.replace`` — which is
        atomic on both POSIX and Windows.
        """
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.backup_dir), prefix=".index-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.index, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.index_file)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

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
                entry = {
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

        shutil.copy2(source, backup_path)

        entry = {
            "filepath": str(source),
            "backup_path": str(backup_path),
            "operation": operation,
            "timestamp": datetime.now().isoformat(),
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
            excess = file_entries[:len(file_entries) - MAX_BACKUPS_PER_FILE]
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


# Lazy-initialized backup store to avoid side effects on import
_backup_store: "FileBackupStore | None" = None


def get_backup_store() -> FileBackupStore:
    """Get or create the global backup store (lazy init)."""
    global _backup_store
    if _backup_store is None:
        _backup_store = FileBackupStore()
    return _backup_store


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
backup_store: FileBackupStore = _LazyBackupStore()  # type: ignore[assignment]


class UndoParams(BaseModel):
    index: Optional[int] = Field(None, description="Index of the operation to undo (0 = most recent). Use undo_history to see available indices.")


class UndoTool(Tool):
    """Tool for undoing file operations."""

    name = "undo"
    description = "Undo a file modification (restores previous version). Optionally specify an index from undo_history."
    parameters_model = UndoParams

    async def execute(self, index: int = None) -> Dict[str, Any]:
        """Undo a file operation."""
        if index is not None:
            return get_backup_store().undo_specific(index)
        return get_backup_store().undo_last()


class UndoHistoryParams(BaseModel):
    limit: int = Field(10, description="Number of entries to show (default: 10)")


class UndoHistoryTool(Tool):
    """Tool for viewing file modification history."""

    name = "undo_history"
    description = "View recent file modification history for undo"
    parameters_model = UndoHistoryParams
    is_read_only = True

    async def execute(self, limit: int = 10) -> Dict[str, Any]:
        """Get undo history."""
        history = get_backup_store().get_history(limit)
        return {
            "success": True,
            "entries": history,
            "count": len(history),
        }
