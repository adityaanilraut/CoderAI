"""CoderAI Tools Package.

Provides all available tools for the coding agent, including:
- Filesystem: read, write, search/replace, apply diff, directory listing, glob,
              move, copy, delete, mkdir
- Terminal: command execution, background processes, process list, kill
- Git (native): status, diff, add, commit, log, branch
- Git (extended via bundled MCP ``git_extended``): push, pull, merge, rebase,
  revert, reset, show, remote, blame, cherry-pick, tag, checkout, stash
- Search: grep with regex, symbol search
- Memory: save, recall, and delete persistent memories
- Web: DuckDuckGo web search, URL reading, file download, HTTP requests
- MCP: connect to external MCP servers
- Undo: file backup and rollback
- Context: pin files to context
- Lint: auto-detect and run project linter
- Tasks: persistent task/TODO list management
- Vision: image reading and analysis
- Sub-agent: delegate tasks to isolated sub-agents
- Skills: load predefined skill workflows (``use_skill``)
- Python REPL: execute Python code in isolated subprocess
- Semantic Search: natural-language codebase search via embeddings
- Desktop: macOS automation tools via AppleScript and Accessibility
"""

from coderAI.tools.base import Tool as Tool, ToolRegistry as ToolRegistry
