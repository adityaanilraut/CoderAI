"""CoderAI Tools Package.

Provides all available tools for the coding agent, including:
- Filesystem: read, write, search/replace, apply diff, directory listing, glob,
              move, copy, delete, mkdir
- Terminal: command execution, background processes, process list, kill
- Git: status, diff, commit, log, branch, checkout, stash,
       push, pull, merge, rebase, revert, reset, show, remote,
       blame, cherry-pick, tag
- Search: text search, grep with regex
- Memory: save, recall, and delete persistent memories
- Web: DuckDuckGo web search, URL reading, file download, HTTP requests
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
    MoveFileTool,
    CopyFileTool,
    DeleteFileTool,
    CreateDirectoryTool,
)

# Terminal tools
from .terminal import RunCommandTool, RunBackgroundTool, ListProcessesTool, KillProcessTool

# Git tools
from .git import (
    GitAddTool, GitStatusTool, GitDiffTool, GitCommitTool, GitLogTool,
    GitBranchTool, GitCheckoutTool, GitStashTool,
    GitPushTool, GitPullTool, GitMergeTool, GitRebaseTool, GitRevertTool,
    GitResetTool, GitShowTool, GitRemoteTool, GitBlameTool,
    GitCherryPickTool, GitTagTool,
)

# Search tools
from .search import TextSearchTool, GrepTool

# Memory tools
from .memory import SaveMemoryTool, RecallMemoryTool, DeleteMemoryTool

# Web search & URL reading & Download & HTTP
from .web import WebSearchTool, ReadURLTool, DownloadFileTool, HTTPRequestTool

# MCP tools
from .mcp import MCPConnectTool, MCPCallTool, MCPListTool, mcp_client

# Undo / rollback tools
from .undo import UndoTool, UndoHistoryTool, get_backup_store, backup_store

# Linter
from .lint import LintTool

# Formatter
from .format import FormatTool

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
    "MoveFileTool",
    "CopyFileTool",
    "DeleteFileTool",
    "CreateDirectoryTool",
    # Terminal
    "RunCommandTool",
    "RunBackgroundTool",
    "ListProcessesTool",
    "KillProcessTool",
    # Git
    "GitAddTool",
    "GitStatusTool",
    "GitDiffTool",
    "GitCommitTool",
    "GitLogTool",
    "GitBranchTool",
    "GitCheckoutTool",
    "GitStashTool",
    "GitPushTool",
    "GitPullTool",
    "GitMergeTool",
    "GitRebaseTool",
    "GitRevertTool",
    "GitResetTool",
    "GitShowTool",
    "GitRemoteTool",
    "GitBlameTool",
    "GitCherryPickTool",
    "GitTagTool",
    # Search
    "TextSearchTool",
    "GrepTool",
    # Memory
    "SaveMemoryTool",
    "RecallMemoryTool",
    "DeleteMemoryTool",
    # Web
    "WebSearchTool",
    "ReadURLTool",
    "DownloadFileTool",
    "HTTPRequestTool",
    # MCP
    "MCPConnectTool",
    "MCPCallTool",
    "MCPListTool",
    "mcp_client",
    # Undo
    "UndoTool",
    "UndoHistoryTool",
    "backup_store",
    "get_backup_store",
    # Linter
    "LintTool",
    # Formatter
    "FormatTool",
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
    # Factory imports (optional for external consumers)
]


