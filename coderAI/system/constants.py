"""Shared filesystem / platform constants used across tools and TUI."""

from __future__ import annotations

import sys

# Directories skipped when walking a project tree (search, indexing, file picker).
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        ".tox",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "vendor",
        "bower_components",
        ".coderAI",
        ".claude",
    }
)


def is_macos() -> bool:
    """Return True when running on macOS (darwin)."""
    return sys.platform == "darwin"
