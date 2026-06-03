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
- Semantic Search: natural-language codebase search via embeddings
"""

from coderAI.tools.base import Tool, ToolRegistry

# Filesystem tools
from coderAI.tools.filesystem import (
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
    FileStatTool,
    FileChmodTool,
    FileChownTool,
    FileReadlinkTool,
)
from coderAI.tools.multi_edit import MultiEditTool

# Terminal tools
from coderAI.tools.terminal import (
    RunCommandTool,
    RunBackgroundTool,
    ListProcessesTool,
    KillProcessTool,
    ReadBgOutputTool,
)

# Git tools
from coderAI.tools.git import (
    GitAddTool,
    GitStatusTool,
    GitDiffTool,
    GitCommitTool,
    GitLogTool,
    GitBranchTool,
    GitCheckoutTool,
    GitStashTool,
    GitPushTool,
    GitPullTool,
    GitMergeTool,
    GitRebaseTool,
    GitRevertTool,
    GitResetTool,
    GitShowTool,
    GitRemoteTool,
    GitBlameTool,
    GitCherryPickTool,
    GitTagTool,
    GitFetchTool,
)

# Search tools
from coderAI.tools.search import TextSearchTool, GrepTool, SymbolSearchTool

# Memory tools
from coderAI.tools.memory import SaveMemoryTool, RecallMemoryTool, DeleteMemoryTool

# Web search & URL reading & Download & HTTP & Feed & Sitemap & Wikipedia
from coderAI.tools.web import (
    WebSearchTool,
    ReadURLTool,
    DownloadFileTool,
    HTTPRequestTool,
    WikipediaSearchTool,
    ReadFeedTool,
    SitemapDiscoverTool,
)

# MCP tools
from coderAI.tools.mcp import (
    MCPConnectTool,
    MCPCallTool,
    MCPListTool,
    MCPDisconnectTool,
    mcp_client,
)

# Undo / rollback tools
from coderAI.tools.undo import UndoTool, UndoHistoryTool, get_backup_store, backup_store

# Linter
from coderAI.tools.lint import LintTool

# Formatter
from coderAI.tools.format import FormatTool

# Project context
from coderAI.tools.project import ProjectContextTool

# Vision
from coderAI.tools.vision import ReadImageTool

# Context management
from coderAI.tools.context_manage import ManageContextTool

# Task management
from coderAI.tools.tasks import ManageTasksTool

# Multi-Agent Sub-agent
from coderAI.tools.subagent import DelegateTaskTool

# Skills
from coderAI.tools.skills import UseSkillTool

# Python REPL
from coderAI.tools.repl import PythonREPLTool

# Planning
from coderAI.tools.planning import CreatePlanTool

# Semantic code search
from coderAI.tools.semantic_search import SemanticSearchTool

# Notepad (inter-agent communication)
from coderAI.tools.notepad import NotepadTool

# Refactoring, testing, and package management (also auto-discovered, but
# exported here so the import surface matches the registered tool list).
from coderAI.tools.refactor import RefactorTool
from coderAI.tools.testing import RunTestsTool
from coderAI.tools.package_manager import PackageManagerTool

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
    "FileStatTool",
    "FileChmodTool",
    "FileChownTool",
    "FileReadlinkTool",
    "MultiEditTool",
    # Terminal
    "RunCommandTool",
    "RunBackgroundTool",
    "ListProcessesTool",
    "KillProcessTool",
    "ReadBgOutputTool",
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
    "GitFetchTool",
    # Search
    "TextSearchTool",
    "GrepTool",
    "SymbolSearchTool",
    # Memory
    "SaveMemoryTool",
    "RecallMemoryTool",
    "DeleteMemoryTool",
    # Web
    "WebSearchTool",
    "ReadURLTool",
    "DownloadFileTool",
    "HTTPRequestTool",
    "WikipediaSearchTool",
    "ReadFeedTool",
    "SitemapDiscoverTool",
    # MCP
    "MCPConnectTool",
    "MCPCallTool",
    "MCPListTool",
    "MCPDisconnectTool",
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
    # Semantic search
    "SemanticSearchTool",
    # Notepad
    "NotepadTool",
    # Refactor, testing, packages
    "RefactorTool",
    "RunTestsTool",
    "PackageManagerTool",
    # Factory imports (optional for external consumers)
]
