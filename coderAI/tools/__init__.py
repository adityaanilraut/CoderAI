"""CoderAI Tools Package.

Provides all available tools for the coding agent, including:
- Filesystem: read, write, search/replace, apply diff, directory listing, glob
- Terminal: command execution, background processes
- Git: status, diff, commit, log, branch, checkout, stash
- Search: text search, grep with regex
- Memory: save and recall persistent memories
- Web: DuckDuckGo web search, URL reading, file download
- MCP: connect to external MCP servers
- Undo: file backup and rollback
- Project: auto-detect project context
- Context: pin files to context
- Lint: auto-detect and run project linter
- Tasks: persistent task/TODO list management
- Vision: image reading and analysis
- Sub-agent: delegate tasks to isolated sub-agents
- Skills: load predefined skill workflows
- Python REPL: execute Python code in isolated subprocess
- Planning: structured plan-and-execute workflows
- Notepad: shared inter-agent communication notepad
"""

from .base import Tool, ToolRegistry

# Filesystem tools
from .filesystem import (
    ReadFileTool,
    WriteFileTool,
    SearchReplaceTool,
    ListDirectoryTool,
    GlobSearchTool,
    ApplyDiffTool,
)

# Terminal tools
from .terminal import RunCommandTool, RunBackgroundTool

# Git tools
from .git import (
    GitAddTool, GitStatusTool, GitDiffTool, GitCommitTool, GitLogTool,
    GitBranchTool, GitCheckoutTool, GitStashTool,
)

# Search tools
from .search import TextSearchTool, GrepTool

# Memory tools
from .memory import SaveMemoryTool, RecallMemoryTool

# Web search & URL reading & Download
from .web import WebSearchTool, ReadURLTool, DownloadFileTool

# MCP tools
from .mcp import MCPConnectTool, MCPCallTool, MCPListTool, mcp_client

# Undo / rollback tools
from .undo import UndoTool, UndoHistoryTool, backup_store

# Linter
from .lint import LintTool

# Project context
from .project import ProjectContextTool

# Vision
from .vision import ReadImageTool

# Context management
from .context_manage import ManageContextTool

# Task management
from .tasks import ManageTasksTool

# Multi-Agent Sub-agent
from .subagent import DelegateTaskTool

# Skills
from .skills import UseSkillTool

# Python REPL
from .repl import PythonREPLTool

# Planning
from .planning import CreatePlanTool

# Notepad (inter-agent communication)
from .notepad import NotepadTool

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
    "ApplyDiffTool",
    # Terminal
    "RunCommandTool",
    "RunBackgroundTool",
    # Git
    "GitAddTool",
    "GitStatusTool",
    "GitDiffTool",
    "GitCommitTool",
    "GitLogTool",
    "GitBranchTool",
    "GitCheckoutTool",
    "GitStashTool",
    # Search
    "TextSearchTool",
    "GrepTool",
    # Memory
    "SaveMemoryTool",
    "RecallMemoryTool",
    # Web
    "WebSearchTool",
    "ReadURLTool",
    "DownloadFileTool",
    # MCP
    "MCPConnectTool",
    "MCPCallTool",
    "MCPListTool",
    "mcp_client",
    # Undo
    "UndoTool",
    "UndoHistoryTool",
    "backup_store",
    # Linter
    "LintTool",
    # Project
    "ProjectContextTool",
    # Context
    "ManageContextTool",
    # Tasks
    "ManageTasksTool",
    # Subagent
    "DelegateTaskTool",
    # Vision
    "ReadImageTool",
    # Skills
    "UseSkillTool",
    # REPL
    "PythonREPLTool",
    # Planning
    "CreatePlanTool",
    # Notepad
    "NotepadTool",
]
