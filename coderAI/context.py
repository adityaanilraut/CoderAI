"""Context management for CoderAI."""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from .config import config_manager

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages project context and pinned files."""

    def __init__(self):
        """Initialize context manager."""
        self.config = config_manager.load()
        self.pinned_files: Dict[str, str] = {}
        self.project_instructions: Optional[str] = None
        
        # Load project instructions
        self._load_instructions()

    def _load_instructions(self):
        """Load project-specific instructions from file."""
        instruction_file = getattr(self.config, "project_instruction_file", "CODERAI.md")
        path = Path(instruction_file)
        if path.exists() and path.is_file():
            try:
                self.project_instructions = path.read_text(encoding="utf-8")
                logger.info(f"Loaded project instructions from {instruction_file}")
            except Exception as e:
                logger.error(f"Failed to load project instructions: {e}")

    def add_file(self, path: str) -> bool:
        """Add a file to pinned context.
        
        Args:
            path: Path to the file to pin
            
        Returns:
            True if successful, False otherwise
        """
        try:
            file_path = Path(path).resolve()
            if not file_path.exists():
                return False
                
            # Basic size check - don't pin huge files
            if file_path.stat().st_size > 100 * 1024:  # 100KB limit for now
                logger.warning(f"File {path} too large to pin")
                return False
                
            content = file_path.read_text(encoding="utf-8")
            self.pinned_files[str(file_path)] = content
            return True
        except Exception as e:
            logger.error(f"Failed to pin file {path}: {e}")
            return False

    def remove_file(self, path: str) -> bool:
        """Remove a file from pinned context.
        
        Args:
            path: Path to remove
            
        Returns:
            True if removed, False if not found
        """
        try:
            # Try exact match first
            if path in self.pinned_files:
                del self.pinned_files[path]
                return True
                
            # Try resolved path
            resolved = str(Path(path).resolve())
            if resolved in self.pinned_files:
                del self.pinned_files[resolved]
                return True
                
            return False
        except Exception:
            return False

    def clear(self):
        """Clear all pinned files."""
        self.pinned_files.clear()

    def get_system_message(self) -> Optional[str]:
        """Get the formatted system message with context.
        
        Returns:
            Formatted string or None if no context
        """
        parts = []
        
        if self.project_instructions:
            parts.append("## Project Instructions")
            parts.append(self.project_instructions)
            parts.append("")
            
        if self.pinned_files:
            parts.append("## Pinned Context Files")
            parts.append("The following files are pinned to the context and should be used as reference:")
            for path, content in self.pinned_files.items():
                parts.append(f"\n### File: {path}")
                parts.append("```")
                parts.append(content)
                parts.append("```")
            parts.append("")
            
        if not parts:
            return None
            
        return "\n".join(parts)

    def get_token_usage_estimate(self) -> int:
        """Estimate token usage of current context."""
        # Rough estimate: 4 chars per token
        text = self.get_system_message() or ""
        return len(text) // 4
