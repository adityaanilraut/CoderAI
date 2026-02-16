"""CoderAI Tools Package.

Provides all available tools for the coding agent, including:
- Filesystem: read, write, search/replace, directory listing, glob
- Terminal: command execution, background processes
- Git: status, diff, commit, log
- Search: text search, grep with regex
- Memory: save and recall persistent memories
- Web: DuckDuckGo web search
- MCP: connect to external MCP servers
- Undo: file backup and rollback
- Project: auto-detect project context
"""

from .base import Tool, ToolRegistry

# Filesystem tools
from .filesystem import (
    ReadFileTool,
    WriteFileTool,
    SearchReplaceTool,
    ListDirectoryTool,
    GlobSearchTool,
)

# Terminal tools
from .terminal import RunCommandTool, RunBackgroundTool

# Git tools
from .git import GitStatusTool, GitDiffTool, GitCommitTool, GitLogTool

# Search tools
from .search import TextSearchTool, GrepTool

# Memory tools
from .memory import SaveMemoryTool, RecallMemoryTool

# Web search
from .web import WebSearchTool

# MCP tools
from .mcp import MCPConnectTool, MCPCallTool, MCPListTool, mcp_client

# Undo / rollback tools
from .undo import UndoTool, UndoHistoryTool, backup_store

# Project context
from .project import ProjectContextTool

__all__ = [
    # Base
    "Tool",
    "ToolRegistry",
    # Filesystem
    "ReadFileTool",
    "WriteFileTool",
    "SearchReplaceTool",
    "ListDirectoryTool",
    "GlobSearchTool",
    # Terminal
    "RunCommandTool",
    "RunBackgroundTool",
    # Git
    "GitStatusTool",
    "GitDiffTool",
    "GitCommitTool",
    "GitLogTool",
    # Search
    "TextSearchTool",
    "GrepTool",
    # Memory
    "SaveMemoryTool",
    "RecallMemoryTool",
    # Web
    "WebSearchTool",
    # MCP
    "MCPConnectTool",
    "MCPCallTool",
    "MCPListTool",
    "mcp_client",
    # Undo
    "UndoTool",
    "UndoHistoryTool",
    "backup_store",
    # Project
    "ProjectContextTool",
]
