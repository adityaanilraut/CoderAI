"""Context management for CoderAI."""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from .config import config_manager
from .context_selector import build_focused_context, summarize_conversation_focus

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages project context and pinned files."""

    def __init__(self, config=None):
        """Initialize context manager."""
        self.config = config.model_copy(deep=True) if config is not None else config_manager.load()
        self.pinned_files: Dict[str, str] = {}
        self._pinned_mtimes: Dict[str, float] = {}  # path -> last known mtime
        self.project_instructions: Optional[str] = None
        self._instructions_loaded: bool = False

    def _load_instructions(self):
        """Load project-specific instructions from file."""
        instruction_file = getattr(self.config, "project_instruction_file", "CODERAI.md")
        project_root = getattr(self.config, "project_root", ".")
        path = Path(project_root) / instruction_file
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
            self._pinned_mtimes[str(file_path)] = file_path.stat().st_mtime
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
                self._pinned_mtimes.pop(path, None)
                return True
                
            # Try resolved path
            resolved = str(Path(path).resolve())
            if resolved in self.pinned_files:
                del self.pinned_files[resolved]
                self._pinned_mtimes.pop(resolved, None)
                return True
                
            return False
        except Exception:
            return False

    def clear(self):
        """Clear all pinned files."""
        self.pinned_files.clear()
        self._pinned_mtimes.clear()

    def refresh_pinned_files(self):
        """Re-read pinned files from disk only when they have changed (mtime check)."""
        stale_keys = []
        for path_str in list(self.pinned_files.keys()):
            try:
                p = Path(path_str)
                if p.exists() and p.is_file():
                    current_mtime = p.stat().st_mtime
                    cached_mtime = self._pinned_mtimes.get(path_str, 0)
                    if current_mtime != cached_mtime:
                        self.pinned_files[path_str] = p.read_text(encoding="utf-8")
                        self._pinned_mtimes[path_str] = current_mtime
                else:
                    stale_keys.append(path_str)
            except Exception as e:
                logger.warning(f"Failed to refresh pinned file {path_str}: {e}")
                stale_keys.append(path_str)
        for key in stale_keys:
            del self.pinned_files[key]
            self._pinned_mtimes.pop(key, None)

    def get_system_message(
        self,
        query: Optional[str] = None,
        messages: Optional[List[dict]] = None,
    ) -> Optional[str]:
        """Get context filtered by relevance to the current task.

        When *query* (or recent *messages*) is provided, only the pinned files
        that are relevant are included, and large files are trimmed to the
        pertinent snippets.  Falls back to full context when no query is given.
        """
        # Lazy-load project instructions on first use so that
        # config.project_root is already set by load_project_config().
        if not self._instructions_loaded:
            self._instructions_loaded = True
            self._load_instructions()

        self.refresh_pinned_files()

        # Derive a relevance query from whatever information we have
        effective_query = query
        if not effective_query and messages:
            effective_query = summarize_conversation_focus(messages)

        # ---- Focused path: relevance-based selection ----
        if effective_query and self.pinned_files:
            focused = build_focused_context(
                files=self.pinned_files,
                query=effective_query,
                project_instructions=self.project_instructions,
                max_total_chars=30_000,
                max_files=5,
            )
            if focused:
                return focused

        # ---- Fallback: include everything (original behaviour) ----
        parts: List[str] = []

        if self.project_instructions:
            parts.append("## Project Instructions")
            parts.append(self.project_instructions)
            parts.append("")

        if self.pinned_files:
            parts.append("## Pinned Context Files")
            parts.append(
                "The following files are pinned to the context and should be used as reference:"
            )
            total_chars = 0
            MAX_FALLBACK_CHARS = 20_000
            for path, content in self.pinned_files.items():
                if len(content) > 10_000:
                    content = content[:10_000] + f"\n... [{len(content) - 10_000} chars truncated to save context]"
                
                if total_chars + len(content) > MAX_FALLBACK_CHARS:
                    parts.append(f"\n### File: {path}")
                    parts.append("```\n... [File omitted to save context. Ask specific questions to view this file.]\n```")
                    continue

                total_chars += len(content)
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
