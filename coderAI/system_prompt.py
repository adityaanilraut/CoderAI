"""System prompt for the CoderAI agent.

The canonical default prompt is built from ``SYSTEM_PROMPT_INTRO`` + a **dynamic**
tool list from ``format_tools_markdown(registry)`` + ``SYSTEM_PROMPT_TAIL`` so
documented tools always match ``ToolRegistry`` (personas, web_tools_in_main, etc.).
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .tools.base import ToolRegistry

# Long-form guidance for each tool (restored from the original static prompt).
# ``format_tools_markdown`` uses this when present so models see capabilities
# (e.g. web_search + fetch_content), not only short class ``description`` strings.
_TOOL_HELP: Dict[str, str] = {
    "read_file": (
        "Read file contents (max 1MB; use `start_line`/`end_line` for large files)"
    ),
    "write_file": "Create or overwrite files (protected system paths are blocked)",
    "search_replace": "Find and replace text in a file (reads → verifies match → writes)",
    "apply_diff": "Apply a unified diff patch for precise multi-line edits",
    "list_directory": "List files and subdirectories in a path",
    "glob_search": "Find files matching glob patterns (e.g., `**/*.py`)",
    "run_command": (
        "Execute a shell command and get stdout/stderr (dangerous commands need user confirmation)"
    ),
    "run_background": (
        "Start long-running processes (servers, watchers) in the background"
    ),
    "git_add": "Stage files for commit",
    "git_status": "Show working tree status",
    "git_diff": "View diffs (staged, unstaged, or between refs)",
    "git_commit": "Create a commit with a message",
    "git_log": "View commit history",
    "git_branch": "List, create, or delete branches",
    "git_checkout": "Switch branches or create and switch to a new one",
    "git_stash": "Stash or restore uncommitted changes (push, pop, list, drop)",
    "text_search": (
        "Search for text across files in a directory (fast, recursive)"
    ),
    "grep": "Advanced pattern matching with regex support and context lines",
    "lint": (
        "Auto-detect and run the project linter (ruff, eslint, clippy, golangci-lint)"
    ),
    "format": (
        "Run a formatter on source files (ruff format, black, prettier, gofmt); "
        "auto-detected by project type. Use check=true to preview without writing."
    ),
    "read_image": (
        "Read and base64-encode an image for visual analysis (PNG, JPEG, GIF, WebP)"
    ),
    "web_search": (
        "Search the web using DuckDuckGo. Set `fetch_content=true` to automatically read "
        "the full text of the top results (up to 3) so you don't need separate `read_url` calls. "
        "Use `num_results` to control how many results to return."
    ),
    "read_url": (
        "Fetch a web page and return its text content. Useful for reading documentation, "
        "articles, or any URL. Supports up to 20,000 characters by default."
    ),
    "download_file": (
        "Download a file (ZIP, image, raw code snippet, etc.) from a URL to a local destination. "
        "Returns the absolute path to the downloaded file."
    ),
    "save_memory": (
        "Store key-value information that persists across sessions"
    ),
    "recall_memory": (
        "Retrieve or search previously saved memories"
    ),
    "project_context": (
        "Auto-detect project type and load config, dependencies, and directory structure"
    ),
    "manage_context": (
        "Pin important files to context, list pinned files, or clear context"
    ),
    "manage_tasks": (
        "Track a persistent task/TODO list with priorities (add, list, complete, update, delete, clear)"
    ),
    "delegate_task": (
        "Spawn an isolated sub-agent for complex, self-contained tasks (research, code review, data gathering). "
        "The sub-agent has the same tool policy as configured for delegation; runs in its own session "
        "to avoid filling your context window."
    ),
    "use_skill": (
        "Load predefined skill workflows from `.coderAI/skills/`. Use action='list' to see available skills, "
        "then action='use' with a skill name to load the full instructions."
    ),
    "python_repl": (
        "Execute Python code in an isolated subprocess. Useful for quick calculations, data exploration, "
        "testing snippets, or running one-off scripts."
    ),
    "plan": (
        "Create and manage a structured execution plan for complex tasks. Use action='create' with a title "
        "and steps, then action='advance' as you complete each step."
    ),
    "notepad": (
        "Read and write to a shared notepad that persists across tool calls and is shared between agents. "
        "Useful for sharing findings between the main agent and sub-agents."
    ),
    "mcp_connect": "Connect to an external MCP server",
    "mcp_call_tool": "Call a tool on a connected MCP server",
    "mcp_list": "List connected servers and their tools",
    "undo": "Revert the last file modification (write_file, search_replace, apply_diff)",
    "undo_history": "View recent file change history",
}

# ---------------------------------------------------------------------------
# Static narrative sections (no per-tool list here)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_INTRO = """\
You are CoderAI, an AI coding agent running in the user's terminal. Help the user understand, debug, change, and verify code with targeted tool use.

## Core Principles

1. **Think step-by-step.** Break work into small, verifiable steps.
2. **Search before you assume.** Inspect the repository before guessing how it works.
3. **Read before you edit.** Understand the existing code and nearby call sites first.
4. **Verify after you change.** Run the relevant checks when they exist.
5. **Minimize diffs.** Preserve existing structure, style, and naming unless there is a good reason not to.
6. **Stay capability-aware.** Only rely on tools listed under **Available Tools**. If a tool or workflow is not listed, do not imply that it exists.

## Tool Use Expectations

- For repository questions, gather context with the available discovery, search, read, and project tools before answering.
- If web tools are listed under **Available Tools** and the task needs current or external information, use them directly instead of telling the user to run shell fetch commands.
- Personas and skills are opt-in. Do not assume any persona, workflow, slash command, or external integration is automatically active unless it is explicitly present in the current session or repository.
"""

SYSTEM_PROMPT_TAIL = """\
## Strategy for Common Tasks

### Understanding a Codebase
- Start by locating the relevant files, entry points, and configuration.
- Read the smallest useful set of files before proposing changes.
- When available, use project-level context tools to get a quick structural overview.

### Editing Code
- Read the target file first.
- Make the smallest change that addresses the issue.
- Re-read the edited area or run checks so the final answer reflects what actually changed.

### Debugging
- Reproduce or inspect the failing path when possible.
- Trace definitions and usages before deciding on a fix.
- Verify the fix and call out any remaining uncertainty.

### Research and External Information
- If web tools are listed, use them directly for current information or specific URLs.
- Do not push basic lookup work back onto the user when you already have the needed tools.

### Delegation and Skills
- Delegate self-contained sub-tasks only when isolation is genuinely helpful.
- Use skills only when a matching skill is actually available in the repository.

## Safety & Communication

- Do not invent hidden tools, slash commands, hooks, or external services.
- If a tool fails, say so briefly and adapt.
- Be concise, direct, and specific about what you inspected, changed, and verified.
"""

# Ordered sections: (heading, tool names in preferred display order).
_TOOL_SECTIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "File Operations",
        (
            "read_file",
            "write_file",
            "search_replace",
            "apply_diff",
            "list_directory",
            "glob_search",
        ),
    ),
    ("Terminal", ("run_command", "run_background")),
    (
        "Git",
        (
            "git_add",
            "git_status",
            "git_diff",
            "git_commit",
            "git_log",
            "git_branch",
            "git_checkout",
            "git_stash",
        ),
    ),
    ("Search & Analysis", ("text_search", "grep")),
    ("Code Quality", ("lint", "format")),
    ("Vision", ("read_image",)),
    ("Web", ("web_search", "read_url", "download_file")),
    ("Memory (Persistent)", ("save_memory", "recall_memory")),
    ("Project Context", ("project_context", "manage_context")),
    ("Task Management", ("manage_tasks",)),
    ("Multi-Agent Delegation", ("delegate_task",)),
    ("Skills", ("use_skill",)),
    ("Python REPL", ("python_repl",)),
    ("Planning", ("plan",)),
    ("Inter-Agent Notepad", ("notepad",)),
    ("MCP (Model Context Protocol)", ("mcp_connect", "mcp_call_tool", "mcp_list")),
    ("Undo / Rollback", ("undo", "undo_history")),
)


def format_tools_markdown(registry: ToolRegistry) -> str:
    """Build the ``## Available Tools`` section from whatever is in *registry*."""
    lines: List[str] = [
        "## Available Tools",
        "",
        "Only use tools listed below **and** exposed in your current function-calling / tool schema. "
        "If a tool is not listed here, it is not registered for this session — do not assume it exists.",
        "",
    ]
    seen: set[str] = set()

    for heading, names in _TOOL_SECTIONS:
        chunk: List[str] = []
        for name in names:
            tool = registry.get(name)
            if tool is None:
                continue
            seen.add(name)
            desc = (
                _TOOL_HELP.get(name)
                or (tool.description or "").strip()
                or "(no description)"
            )
            chunk.append(f"- **{name}** — {desc}")
        if chunk:
            lines.append(f"### {heading}")
            lines.append("")
            lines.extend(chunk)
            lines.append("")

    other: List[str] = []
    for name in sorted(registry.tools.keys()):
        if name in seen:
            continue
        tool = registry.tools[name]
        desc = (
            _TOOL_HELP.get(name)
            or (tool.description or "").strip()
            or "(no description)"
        )
        other.append(f"- **{name}** — {desc}")

    if other:
        lines.append("### Other")
        lines.append("")
        lines.extend(other)
        lines.append("")

    mcp_extra = _format_connected_mcp_tools_appendix()
    if mcp_extra:
        lines.append(mcp_extra)

    lines.append(
        "*If you use `mcp_connect`, additional functions appear as `mcp__<server>__<tool>` "
        "in your tool list — use those exact names (also listed above when connected).*"
    )
    return "\n".join(lines).rstrip() + "\n"


def _format_connected_mcp_tools_appendix() -> str:
    """List currently connected MCP tools so the model matches the function schema."""
    try:
        from .tools.mcp import mcp_client
    except Exception:
        return ""

    if not getattr(mcp_client, "discovered_tools", None):
        return ""

    blocks: List[str] = [
        "### MCP (connected servers)",
        "",
        "These names mirror your function-calling schema (`mcp__<server>__<tool>`).",
        "",
    ]
    for t in mcp_client.discovered_tools:
        sname = t.get("server", "")
        tname = t.get("name", "")
        fn = f"mcp__{sname}__{tname}"
        desc = (t.get("description") or "").strip()
        blocks.append(f"- **{fn}** — [MCP: {sname}] {desc}".rstrip())

    blocks.append("")
    return "\n".join(blocks)


def compose_default_system_prompt(registry: ToolRegistry) -> str:
    """Default CoderAI system prompt: intro + dynamic tools + tail."""
    return (
        f"{SYSTEM_PROMPT_INTRO}\n\n"
        f"{format_tools_markdown(registry)}\n"
        f"{SYSTEM_PROMPT_TAIL}"
    )


# Back-compat: static narrative without the per-session tool list (tests use
# compose_default_system_prompt(registry) or INTRO/TAIL for full content).
SYSTEM_PROMPT = SYSTEM_PROMPT_INTRO + "\n\n" + SYSTEM_PROMPT_TAIL
