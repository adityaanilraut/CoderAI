"""Undo / rollback tool for reverting file changes."""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .base import Tool

logger = logging.getLogger(__name__)

# Maximum number of backups to keep per file
MAX_BACKUPS_PER_FILE = 10


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
        """Save backup index to disk."""
        with open(self.index_file, "w") as f:
            json.dump(self.index, f, indent=2)

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

        # Clean up old backups for this file
        self._cleanup_old_backups(str(source))

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

        except Exception as e:
            return {"success": False, "error": str(e)}

        return {"success": False, "error": "Unknown operation type"}

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
            to_remove = file_entries[: len(file_entries) - MAX_BACKUPS_PER_FILE]
            for entry in to_remove:
                if entry.get("backup_path"):
                    backup = Path(entry["backup_path"])
                    if backup.exists():
                        backup.unlink()
                self.index.remove(entry)
            self._save_index()


# Global backup store
backup_store = FileBackupStore()


class UndoTool(Tool):
    """Tool for undoing the last file operation."""

    name = "undo"
    description = "Undo the last file modification (restores previous version)"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self) -> Dict[str, Any]:
        """Undo the last file operation."""
        return backup_store.undo_last()


class UndoHistoryTool(Tool):
    """Tool for viewing file modification history."""

    name = "undo_history"
    description = "View recent file modification history for undo"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of entries to show (default: 10)",
                },
            },
        }

    async def execute(self, limit: int = 10) -> Dict[str, Any]:
        """Get undo history."""
        history = backup_store.get_history(limit)
        return {
            "success": True,
            "entries": history,
            "count": len(history),
        }
