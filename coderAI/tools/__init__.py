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
- Desktop: macOS automation tools via AppleScript and Accessibility
"""

from coderAI.tools.base import Tool as Tool, ToolRegistry as ToolRegistry
