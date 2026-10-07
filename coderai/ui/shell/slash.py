from __future__ import annotations

from typing import Any
from coderai.sandbox import SANDBOX_MODES
from coderai.ui.shell.shortcuts import shortcut_help

from coderai.utils.slashcmd import (
    SlashCommand,
    SlashCommandCall,
    SlashCommandRegistry,
    parse_slash_command_call,
)

__all__ = [
    "SlashCommand",
    "SlashCommandCall",
    "SlashCommandRegistry",
    "parse_slash_command_call",
]

_COMMANDS = (
    SlashCommand("stop", "Interrupt the active turn; pause queued prompts", "Planning & Safety"),
    SlashCommand(
        "steer", "Steer the active turn with text (send normally when idle)", "Input & Media"
    ),
    SlashCommand(
        "queue",
        "List, reorder, remove, clear, or resume queued prompts",
        "Input & Media",
        subcommands=("list", "edit", "remove", "move", "clear", "run"),
    ),
    SlashCommand(
        "retry", "Retry the last failed submission in the current workspace", "Planning & Safety"
    ),
    SlashCommand(
        "output", "Inspect complete tool output by message or tool ID", "Tools & Analytics"
    ),
    SlashCommand("activity", "Inspect foreground work, jobs, and subagents", "Diagnostics"),
    SlashCommand("attach", "Prepare a quoted file mention in the composer", "Input & Media"),
    SlashCommand(
        "attachments",
        "Review or remove draft files and images",
        "Input & Media",
        subcommands=("edit", "remove", "clear"),
    ),
    SlashCommand(
        "display",
        "Persist detail and accessibility preferences",
        "Utilities",
        subcommands=("compact", "verbose", "accessible", "animated"),
    ),
    SlashCommand("new", "Start a fresh session", "Session Management"),
    SlashCommand("init", "Initialize or update AGENTS.md guidelines", "Session Management"),
    SlashCommand(
        "sessions",
        "Search all saved sessions and resume a selection",
        "Session Management",
        aliases=("resume",),
        subcommands=("manage", "pin", "unpin", "archive", "unarchive", "tree"),
    ),
    SlashCommand("fork", "Fork the current or specified session", "Session Management"),
    SlashCommand("delete", "Delete a saved session", "Session Management", ("rm",)),
    SlashCommand("rename", "View or set the session title", "Session Management", ("title",)),
    SlashCommand("export", "Export session history to Markdown or JSON", "Session Management"),
    SlashCommand("login", "Log in / configure API platform (OAuth or API key)", "Account"),
    SlashCommand("logout", "Log out from the current platform", "Account"),
    SlashCommand("version", "Show CLI version", "Help & Info"),
    SlashCommand("changelog", "Show recent changelog", "Help & Info", ("release-notes",)),
    SlashCommand("feedback", "Submit feedback (falls back to GitHub Issues)", "Help & Info"),
    SlashCommand("reload", "Reload configuration without exiting", "Help & Info"),
    SlashCommand("debug", "Show context debug info (msgs/tokens/checkpoints)", "Help & Info"),
    SlashCommand(
        "usage",
        "Show locally recorded usage; provider quota is unavailable",
        "Help & Info",
        ("status", "quota"),
    ),
    SlashCommand(
        "plan",
        "Toggle or apply Plan Mode",
        "Planning & Safety",
        subcommands=("on", "off", "view", "clear", "apply", "reset"),
    ),
    SlashCommand("undo", "Revert to a previous checkpoint", "Planning & Safety"),
    SlashCommand("diff", "Show the current unified diff", "Planning & Safety"),
    SlashCommand(
        "review",
        "Review uncommitted changes (Jev triage, model review, Jev gate)",
        "Planning & Safety",
        subcommands=("--all", "--untracked", "--no-untracked"),
    ),
    SlashCommand("continue", "Continue agent execution", "Planning & Safety"),
    SlashCommand("yolo", "Toggle YOLO auto-approve all actions", "Planning & Safety"),
    SlashCommand("afk", "Toggle AFK auto-dismiss questions & approvals", "Planning & Safety"),
    SlashCommand(
        "add-dir",
        "Add directory to workspace",
        "Planning & Safety",
        aliases=("add_dir",),
    ),
    SlashCommand(
        "setup",
        "Configure API keys, providers, endpoints, and default model",
        "Models & Reasoning",
        ("auth", "keys", "configure"),
        ("quick", "keys", "models", "provider", "test", "status"),
    ),
    SlashCommand(
        "model",
        "Select or switch the active model",
        "Models & Reasoning",
        aliases=("models",),
        subcommands=("favorites", "recent", "favorite", "unfavorite", "verify", "refresh"),
    ),
    SlashCommand(
        "effort",
        "Select reasoning effort",
        "Models & Reasoning",
        ("reasoning",),
        ("off", "low", "medium", "high", "xhigh", "max"),
    ),
    SlashCommand(
        "thinking",
        "Toggle reasoning trace display",
        "Models & Reasoning",
        ("raw",),
        ("full", "summary", "lite", "normal", "on", "off"),
    ),
    SlashCommand("skills", "Browse discovered skills", "Models & Reasoning"),
    SlashCommand("skill", "Load a skill into this session", "Models & Reasoning"),
    SlashCommand("doctor", "Run system and connectivity diagnostics", "Diagnostics"),
    SlashCommand(
        "jobs",
        "Inspect and manage background jobs",
        "Diagnostics",
        ("job",),
        ("list", "kill", "logs"),
    ),
    SlashCommand(
        "schedule",
        "Manage reminders and timers",
        "Diagnostics",
        subcommands=("list", "after", "at", "every", "cancel"),
    ),
    SlashCommand(
        "agents",
        "List live subagent runs or discovered roles",
        "Diagnostics",
        ("subagents", "subagent"),
        ("list", "roles", "tree", "report", "send"),
    ),
    SlashCommand(
        "teams",
        "Inspect active agent teams",
        "Diagnostics",
        aliases=("team", "swarm"),
    ),
    SlashCommand(
        "web-vis",
        "Pure CLI notice for removed web/browser UI",
        "Utilities",
        aliases=("web", "vis", "browser", "web_vis"),
    ),
    SlashCommand(
        "mcp",
        "Inspect MCP servers, tools, prompts, and resources",
        "Tools & Analytics",
        subcommands=("reconnect", "prompts", "resources"),
    ),
    SlashCommand("tokens", "Show token usage", "Tools & Analytics", ("cost",)),
    SlashCommand(
        "tools", "Inspect effective tools, schemas, and restrictions", "Tools & Analytics"
    ),
    SlashCommand(
        "compact", "Compress conversation context", "Tools & Analytics", subcommands=("keep",)
    ),
    SlashCommand("history", "Show the session timeline", "Tools & Analytics"),
    SlashCommand("import", "Import context from a file", "Tools & Analytics"),
    SlashCommand("config", "Show resolved configuration", "Tools & Analytics", ("settings",)),
    SlashCommand(
        "permission",
        "Show or set the permission preset",
        "Tools & Analytics",
        ("permissions",),
        SANDBOX_MODES,
    ),
    SlashCommand(
        "goal",
        "List or update session goals",
        "Tools & Analytics",
        subcommands=("list", "add", "start", "pause", "done", "cancel"),
    ),
    SlashCommand("image", "Attach an image for analysis", "Input & Media"),
    SlashCommand("editor", "Compose a prompt in $EDITOR", "Input & Media", ("edit",)),
    SlashCommand("paste", "Enter multiline paste mode", "Input & Media"),
    SlashCommand("clear", "Clear the terminal", "Utilities"),
    SlashCommand("reset", "Clear conversation context and reset session state", "Utilities"),
    SlashCommand("context", "Inspect live context window utilization", "Tools & Analytics"),
    SlashCommand("theme", "Switch theme dark/light", "Utilities", subcommands=("dark", "light")),
    SlashCommand("task", "Open interactive background-task browser", "Utilities"),
    SlashCommand(
        "agent",
        "View or switch active agent role",
        "Models & Reasoning",
        ("role", "roles"),
        ("roles", "switch", "list"),
    ),
    SlashCommand("upgrade", "Check for and install CoderAI updates", "Utilities"),
    SlashCommand("hooks", "Show configured hooks", "Utilities"),
    SlashCommand(
        "btw", "Ask a side question without changing conversation history", "Utilities", ("side",)
    ),
    SlashCommand("help", "Show command help", "Utilities", ("?", "h")),
    SlashCommand("exit", "Exit CoderAI", "Utilities", ("quit",)),
)

COMMAND_CATALOG = {command.name: command for command in _COMMANDS}
COMMAND_ALIASES = {alias: command.name for command in _COMMANDS for alias in command.aliases}


def resolve_command(name: str) -> SlashCommand | None:
    """Resolve a command or alias without its leading slash."""
    key = name.strip().lower().lstrip("/")
    cmd = COMMAND_CATALOG.get(COMMAND_ALIASES.get(key, key))
    if cmd is not None:
        return cmd
    try:
        from coderai.ui.shell.dispatch import registry

        return registry.find_command(key)
    except Exception:
        return None


def parse_slash_command(raw: str) -> tuple[str, str]:
    """Return a canonical slash command and its unmodified argument text."""
    call = parse_slash_command_call(raw)
    if call is None:
        # Lenient fallback so aliases like /? still resolve.
        command_text, _, argument = raw.strip().partition(" ")
        resolved = resolve_command(command_text)
        canonical = resolved.name if resolved else command_text.lower().lstrip("/")
        return f"/{canonical}", argument.strip()
    if ":" in call.name:
        # Preserve suffix case for dynamic /skill:<name> and /flow:<name>.
        base, _, suffix = call.name.partition(":")
        resolved = resolve_command(base)
        canonical_base = resolved.name if resolved else base.lower()
        return f"/{canonical_base}:{suffix}", call.args.strip()
    resolved = resolve_command(call.name)
    canonical = resolved.name if resolved else call.name.lower()
    return f"/{canonical}", call.args.strip()


def completion_entries() -> list[tuple[str, str]]:
    """Return canonical commands and aliases for readline completion."""
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for command in _COMMANDS:
        entries.append((f"/{command.name}", command.summary))
        seen.add(command.name)
        for alias in command.aliases:
            entries.append((f"/{alias}", f"{command.summary} (alias)"))
            seen.add(alias)

    try:
        from coderai.ui.shell.dispatch import registry

        for trig, cmd in registry.iter_command_entries():
            if trig not in seen:
                is_alias = trig != cmd.name
                suffix = " (alias)" if is_alias else ""
                desc = cmd.description or cmd.summary or ""
                entries.append((f"/{trig}", f"{desc}{suffix}"))
                seen.add(trig)
    except Exception:
        pass

    return entries


"""Clean, modern, and simple slash-command help system for CoderAI CLI."""


import difflib


_RICH = True
try:
    from rich.align import Align
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    _RICH = False
    Align = None  # type: ignore[assignment,misc]
    Console = None  # type: ignore[assignment,misc]
    Panel = None  # type: ignore[assignment,misc]
    Table = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]

    def escape(text: str) -> str:  # type: ignore[misc]
        return text


COMMAND_HELP_DETAILS: dict[str, dict[str, Any]] = {
    "setup": {
        "title": "Setup & API Keys Manager",
        "syntax": "/setup [quick|keys|models|provider|test|status] (aliases: /auth, /keys, /configure)",
        "summary": "Configure API keys, providers, local endpoints, and default active model.",
        "description": (
            "Opens the interactive configuration wizard for LLM providers and models.\n"
            "• /setup              — Launch full interactive setup wizard\n"
            "• /setup quick        — Guided 3-step setup walkthrough\n"
            "• /setup keys         — Configure API keys (OpenAI, DeepSeek, Gemini, Anthropic, OpenRouter)\n"
            "• /setup models       — Select or customize default active model\n"
            "• /setup provider     — Configure custom/local endpoint (Ollama, LM Studio, vLLM, Groq)\n"
            "• /setup test         — Test live connection & authentication with active model\n"
            "• /setup status       — View key configuration and masked credentials summary"
        ),
        "examples": [
            "/setup",
            "/setup keys",
            "/setup quick",
            "/setup test",
            "/setup status",
        ],
    },
    "plan": {
        "title": "Plan Mode",
        "syntax": "/plan [on|off|view|clear|apply|reset]",
        "summary": "Toggle or manage Plan Mode (strict read-only safety boundary).",
        "description": (
            "Plan Mode enforces a strict read-only boundary where the agent analyzes the codebase "
            "and formulates an architectural implementation plan without mutating files or running destructive commands.\n"
            "• /plan        — Toggle Plan Mode on/off\n"
            "• /plan on     — Turn on Plan Mode\n"
            "• /plan off    — Turn off Plan Mode\n"
            "• /plan apply  — Approve plan, turn off Plan Mode, and begin implementation\n"
            "• /plan view   — Show the recorded plan\n"
            "• /plan clear   — Clear the recorded plan and exit Plan Mode\n"
            "• /plan reset  — Reset plan mode state to default (alias of clear)"
        ),
        "examples": ["/plan", "/plan on", "/plan apply", "/plan reset"],
    },
    "goal": {
        "title": "Session Goals",
        "syntax": "/goal [list|add <objective>|start <id>|pause <id>|done <id>|cancel <id>]",
        "summary": "Save session goals and run bounded attempts.",
        "description": (
            "Track high-level goals and progress for the current session.\n"
            "• /goal                      — List all goals for active session\n"
            "• /goal add <objective>      — Save a pending goal\n"
            "• /goal done <id>            — Mark goal as completed\n"
            "• /goal cancel <id>          — Cancel goal\n"
            "• /goal start <id>           — Run or resume a goal within its round budget\n"
            "• /goal pause <id>           — Pause goal execution"
        ),
        "examples": [
            "/goal",
            "/goal add Refactor database migration logic",
            "/goal done a1b2c3d4",
            "/goal cancel a1b2c3d4",
        ],
    },
    "mcp": {
        "title": "Model Context Protocol (MCP)",
        "syntax": "/mcp [prompts|resources [uri]|reconnect <server_name>]",
        "summary": "Inspect and manage MCP servers, tools, prompts, and resources.",
        "description": (
            "Inspect connected MCP servers, tool definitions, prompts, and resources.\n"
            "• /mcp                      — Open interactive MCP server & tools inspector\n"
            "• /mcp prompts              — Browse MCP server prompts\n"
            "• /mcp resources [uri]      — Inspect MCP resources or read resource by URI\n"
            "• /mcp reconnect <server>   — Reconnect a failed or disconnected MCP server"
        ),
        "examples": ["/mcp", "/mcp prompts", "/mcp resources", "/mcp reconnect github"],
    },
    "permission": {
        "title": "Permissions & Sandbox",
        "syntax": "/permission [preset] (alias: /permissions)",
        "summary": "Show or set permission preset and sandbox boundary.",
        "description": (
            "Configure runtime tool execution permissions for bash and file operations.\n"
            "Presets:\n"
            "• read-only            — Only allow read tools; ask before modifications\n"
            "• workspace-write      — Allow workspace edits; ask for external/dangerous commands\n"
            "• danger-full-access   — Allow all tools without confirmation prompts\n"
            "Changes are saved to the project and apply to new sessions."
        ),
        "examples": [
            "/permission",
            "/permission workspace-write",
            "/permission danger-full-access",
        ],
    },
    "doctor": {
        "title": "System Doctor Diagnostics",
        "syntax": "/doctor",
        "summary": "Run comprehensive system health checks across environment, credentials, MCP, and storage.",
        "description": (
            "Runs diagnostic probes verifying:\n"
            "1. Python runtime, version, and virtualenv status\n"
            "2. Git repository root, branch, and working tree dirty state\n"
            "3. Active model LLM credentials and endpoint connectivity\n"
            "4. Connected and failed MCP servers & registered tools\n"
            "5. Workspace and global discovered skills\n"
            "6. Storage permissions for .coderai and ~/.coderai\n"
            "7. Active background jobs and scheduled reminders"
        ),
        "examples": ["/doctor"],
    },
    "jobs": {
        "title": "Background Jobs",
        "syntax": "/jobs or /job [list|kill <id>|logs <id>]",
        "summary": "Inspect and control async background jobs.",
        "description": (
            "View and manage long-running background bash commands and processes.\n"
            "• /jobs or /job             — List active and recent background jobs\n"
            "• /job kill <id>            — Terminate a running background job\n"
            "• /job logs <id>            — View output log tail for a background job"
        ),
        "examples": ["/jobs", "/job list", "/job kill job_1", "/job logs job_1"],
    },
    "schedule": {
        "title": "Scheduled Reminders & Timers",
        "syntax": "/schedule [list|after <sec> <prompt>|at <iso> <prompt>|every <sec> <prompt>|cancel <id>]",
        "summary": "Schedule one-off reminders or recurring background timers.",
        "description": (
            "Manage session-scoped scheduled timers and reminders.\n"
            "• /schedule                         — List scheduled timers\n"
            "• /schedule after <sec> <prompt>    — Schedule reminder after N seconds\n"
            "• /schedule at <iso> <prompt>       — Schedule reminder at ISO datetime\n"
            "• /schedule every <sec> <prompt>    — Schedule recurring reminder (min 300s)\n"
            "• /schedule cancel <id>             — Cancel a scheduled reminder"
        ),
        "examples": [
            "/schedule",
            "/schedule after 300 Check on background build progress",
            "/schedule cancel 1",
        ],
    },
    "agents": {
        "title": "Subagents & Multi-Agent Hierarchy",
        "syntax": "/agents [list|roles|tree|report <id>|send <id> <message>]",
        "summary": "Inspect live subagent runs, or list discovered roles.",
        "description": (
            "Inspect delegated subagents in the current session.\n"
            "• /agents                   — List the live subagent tree\n"
            "• /agents roles             — List bundled and discovered agent roles\n"
            "• /agents report <id>       — Show a live agent's report\n"
            "• /agents send <id> <msg>   — Queue a follow-up for a live agent"
        ),
        "examples": [
            "/agents",
            "/agents roles",
            "/agents report agt_123",
            "/agents send agt_123 Check the failing test",
        ],
    },
    "teams": {
        "title": "Multi-Agent Teams",
        "syntax": "/teams",
        "summary": "Inspect active agent team members, task allocation, and coordination boards.",
        "description": "View team configuration, assigned tasks, and message routing among specialized agents.",
        "examples": ["/teams"],
    },
    "image": {
        "title": "Image Vision Attachment",
        "syntax": "/image <path> [prompt]",
        "summary": "Attach an image file for vision analysis accompanying your turn.",
        "description": (
            "Encodes and attaches an image (PNG, JPEG, WebP, GIF) to your message with multimodal support.\n"
            "Quoted paths support spaces. In the interactive shell, the image and prompt return to the composer for review; Enter submits. Ctrl-T reviews or removes files and images."
        ),
        "examples": [
            "/image docs/diagram.png Explain this architectural design",
            "/image ./ui-screenshot.png Find visual layout errors",
        ],
    },
    "rename": {
        "title": "Rename Session",
        "syntax": "/rename [new_title] or /rename <session_id> <new_title>",
        "summary": "Rename the summary/title of the active or specified session.",
        "description": "Updates the session summary displayed in /sessions and history logs.",
        "examples": [
            "/rename Implement Auth Service",
            "/rename sess_abc123 Refactor Database Layer",
        ],
    },
    "editor": {
        "title": "External $EDITOR Integration",
        "syntax": "/editor or /edit",
        "summary": "Compose or edit your prompt in your configured $EDITOR (nano, vim, vi, code, etc.).",
        "description": (
            "Opens your system default or configured $EDITOR in a temporary markdown file.\n"
            "When you save and close the editor, the content returns to the composer for review and submission."
        ),
        "examples": ["/editor", "/edit"],
    },
    "paste": {
        "title": "Multiline Paste Mode",
        "syntax": "/paste",
        "summary": "Enter multiline paste mode for long code snippets or text blocks.",
        "description": "Returns pasted text to the composer for review. Ctrl-J inserts a newline; Enter finishes capture. Large pastes collapse visually while preserving the full submitted text. Esc, Ctrl-C, and EOF cancel capture.",
        "examples": ["/paste"],
    },
    "model": {
        "title": "Model",
        "syntax": "/model [provider|model ID|favorites|recent|favorite [id]|unfavorite [id]|verify|refresh]",
        "summary": "Compare configured providers, cached capabilities, estimated pricing, and context limits",
        "description": "Compare configured providers, cached capabilities, estimated pricing, and context limits. Favorite and recent collections are workspace-specific. /model verify explicitly tests the active connection; menu rendering never probes providers.",
        "examples": ["/model"],
    },
    "effort": {
        "title": "Reasoning Effort Selection",
        "syntax": "/effort [off|low|medium|high|xhigh|max]",
        "summary": "Select or switch reasoning effort level (thinking token budget).",
        "description": "Opens interactive reasoning effort menu or sets effort tier (off, low, medium, high, xhigh, max).",
        "examples": [
            "/effort",
            "/effort max",
            "/effort high",
            "/effort medium",
            "/effort low",
            "/effort off",
        ],
    },
    "reasoning": {
        "title": "Reasoning Effort Selection (Alias)",
        "syntax": "/reasoning [off|low|medium|high|xhigh|max]",
        "summary": "Alias for /effort.",
        "description": "Select or switch reasoning effort level.",
        "examples": ["/reasoning", "/reasoning max"],
    },
    "sessions": {
        "title": "Sessions",
        "syntax": "/sessions [search|manage [id]|pin|unpin|archive|unarchive|tree]",
        "summary": "Search saved sessions or resume an ID",
        "description": "Search saved sessions or resume an ID. Pin/archive actions accept an ID (default: current). Filters: status:ready, after:YYYY-MM-DD, before:YYYY-MM-DD, pinned:true, archived:only or archived:all, fork:<id>. Add tree to navigate fork ancestry. /sessions manage <id> opens organization actions.",
        "examples": ["/sessions"],
    },
    "undo": {
        "title": "Undo",
        "syntax": "/undo",
        "summary": "Choose a checkpoint and restoration scope, inspect the affected files and conversation messages, then confirm",
        "description": "Choose a checkpoint and restoration scope, inspect the affected files and conversation messages, then confirm. Unavailable file checkpoints cannot be selected. Esc cancels.",
        "examples": ["/undo"],
    },
    "diff": {
        "title": "Diff",
        "syntax": "/diff",
        "summary": "Browse complete session changes by file and hunk",
        "description": "Browse complete session changes by file and hunk. Enter opens searchable full output; Ctrl-F searches; Esc returns. Counts always describe the complete diff.",
        "examples": ["/diff"],
    },
    "review": {
        "title": "Code Review",
        "syntax": "/review [<base-ref>] [--all] [--untracked]",
        "summary": "Review changes: Jev triages files, the active model reviews, Jev filters comments.",
        "description": (
            "Diffs the working tree (tracked files only, unless --untracked) against HEAD, or against "
            "merge-base(<base-ref>, HEAD). Jev System-One screens each file and skips low-risk "
            "ones; the active chat model reviews the rest; Jev then drops comments it scores as "
            "speculative or unlikely to be accepted. Without TYPESAFE_API_KEY every non-doc file "
            "is reviewed and every comment is shown. • --all — review every file regardless of "
            "triage • --untracked — also include untracked files (dotfiles and secret-looking "
            "paths are always skipped) • CODERAI_REVIEW_MAX_CHARS caps "
            "the diff sent to the model."
        ),
        "examples": ["/review", "/review main", "/review origin/main --all"],
    },
    "continue": {
        "title": "Continue Execution",
        "syntax": "/continue",
        "summary": "Continue bounded multi-step agent execution.",
        "description": "Instructs the agent to resume automated tool executions and multi-step plan progress.",
        "examples": ["/continue"],
    },
    "export": {
        "title": "Export Session",
        "syntax": "/export [file.md|file.json]",
        "summary": "Export session conversation history to Markdown or JSON.",
        "description": "Exports turn timeline, tool calls, and assistant replies to a standalone file.",
        "examples": ["/export", "/export session_notes.md", "/export session_dump.json"],
    },
    "tokens": {
        "title": "Token Usage & Cost Analytics",
        "syntax": "/tokens (alias: /cost)",
        "summary": "Display detailed token usage breakdown and cost estimation.",
        "description": "Inspect prompt tokens, completion tokens, cached tokens, and turn usage.",
        "examples": ["/tokens", "/cost"],
    },
    "config": {
        "title": "Configuration & Settings",
        "syntax": "/config (alias: /settings)",
        "summary": "Inspect resolved workspace and user settings.",
        "description": "Displays resolved settings from project and user settings files.",
        "examples": ["/config", "/settings"],
    },
    "compact": {
        "title": "Context Compaction",
        "syntax": "/compact",
        "summary": "Compress conversation history to free up active context tokens.",
        "description": "Summarizes past conversation turns to preserve token budget.",
        "examples": ["/compact"],
    },
    "history": {
        "title": "Turn Timeline History",
        "syntax": "/history",
        "summary": "View turn-by-turn conversation timeline.",
        "description": "Search the full persisted transcript, including complete messages, tool results, reasoning, and compaction boundaries. Enter opens a message; Ctrl-F searches inside it. Esc returns without losing the browser query and selection.",
        "examples": ["/history"],
    },
    "skills": {
        "title": "Skills & Customizations",
        "syntax": "/skills or /skill <name>",
        "summary": "Explore and load workspace & global skills.",
        "description": "Lists available skills or loads specialized skill instructions into current session.",
        "examples": ["/skills", "/skill agy-customizations"],
    },
    "skill": {
        "title": "Load Workspace Skill",
        "syntax": "/skill <name>",
        "summary": "Load a specialized skill into the current session.",
        "description": "Loads the instructions and resources of a discovered skill into active context.",
        "examples": ["/skill agy-customizations", "/skill modern-web-guidance"],
    },
    "thinking": {
        "title": "Reasoning Trace Display",
        "syntax": "/thinking [full|summary|lite|normal|on|off] (alias: /raw)",
        "summary": "Toggle full reasoning trace or concise summary.",
        "description": "Controls display mode for model reasoning traces (expanded or compact summary).",
        "examples": ["/thinking", "/thinking full", "/thinking summary", "/raw"],
    },
    "clear": {
        "title": "Clear Screen",
        "syntax": "/clear",
        "summary": "Clear terminal screen and redraw status bar.",
        "description": "Clears screen buffer while keeping session state intact.",
        "examples": ["/clear"],
    },
    "new": {
        "title": "New Session",
        "syntax": "/new",
        "summary": "Start a fresh session in the workspace.",
        "description": "Initializes a clean session without carrying over past turn history.",
        "examples": ["/new"],
    },
    "init": {
        "title": "Initialize Guidelines",
        "syntax": "/init",
        "summary": "Initialize or update AGENTS.md contributor guidelines for the project.",
        "description": "Creates or updates AGENTS.md with architecture and coding standards.",
        "examples": ["/init"],
    },
    "resume": {
        "title": "Resume Session",
        "syntax": "/resume <session_id>",
        "summary": "Resume a saved session by ID directly.",
        "description": "Loads past conversation history and state for the specified session ID.",
        "examples": ["/resume sess_0123456789ab"],
    },
    "fork": {
        "title": "Fork Session",
        "syntax": "/fork [session_id]",
        "summary": "Fork current or target session into a new branch.",
        "description": "Creates a new session branch cloning message history and git file checkpoints.",
        "examples": ["/fork", "/fork sess_0123456789ab"],
    },
    "delete": {
        "title": "Delete Session",
        "syntax": "/delete <session_id> (alias: /rm)",
        "summary": "Delete a saved session from workspace.",
        "description": "Removes session messages and index entry.",
        "examples": ["/delete sess_0123456789ab", "/rm sess_0123456789ab"],
    },
    "exit": {
        "title": "Exit Session",
        "syntax": "/exit or /quit",
        "summary": "Exit CoderAI session with summary card.",
        "description": "Terminates the interactive REPL session and displays a summary of turns and token usage.",
        "examples": ["/exit", "/quit"],
    },
    "help": {
        "title": "Help Cheatsheet",
        "syntax": "/help [command] (alias: /?)",
        "summary": "Show command help menu or detailed contextual help.",
        "description": "Displays the categorized command cheatsheet or in-depth details for a specific command.",
        "examples": ["/help", "/help plan", "/help setup", "/help shortcuts", "/?"],
    },
    "shortcuts": {
        "title": "Keyboard Shortcuts & Controls",
        "syntax": "/help shortcuts",
        "summary": "Key bindings and interactive controls cheatsheet.",
        "description": shortcut_help(),
        "examples": ["/help shortcuts"],
    },
    "theme": {
        "title": "Theme Switch",
        "syntax": "/theme [dark|light]",
        "summary": "Switch and persist the terminal theme (dark/light).",
        "description": "Themes apply to the composer, focused browsers, output inspector, transcript, semantic statuses, and diffs. /theme with no argument shows the current theme.",
        "examples": ["/theme", "/theme dark", "/theme light"],
    },
    "btw": {
        "title": "Side Question (BTW)",
        "syntax": "/btw <question>",
        "summary": "Ask a side question without changing conversation history.",
        "description": "Start when the foreground turn is idle. The composer remains available; /stop interrupts the side question and Enter queues a follow-up.",
        "examples": ["/btw What does this error mean?", "/btw Summarize the file"],
    },
    "login": {
        "title": "Log In",
        "syntax": "/login",
        "summary": "Log in / configure API platform (OAuth or API key), then pick model.",
        "description": "Platform select, API key entry, model selection, save + reload.",
        "examples": ["/login"],
    },
    "logout": {
        "title": "Log Out",
        "syntax": "/logout",
        "summary": "Log out: clear stored provider credentials and reload.",
        "description": "Strips api_key values from settings and clears CODERAI_*KEY env vars.",
        "examples": ["/logout"],
    },
    "version": {
        "title": "Version",
        "syntax": "/version",
        "summary": "Show CLI version.",
        "description": "Prints the installed coderai-agent version.",
        "examples": ["/version"],
    },
    "changelog": {
        "title": "Changelog",
        "syntax": "/changelog (alias: /release-notes)",
        "summary": "Show recent changelog entries.",
        "description": "Renders the top CHANGELOG.md sections.",
        "examples": ["/changelog"],
    },
    "feedback": {
        "title": "Feedback",
        "syntax": "/feedback [text]",
        "summary": "Submit feedback (falls back to GitHub Issues).",
        "description": "Prompts for feedback and opens a prefilled GitHub issue URL.",
        "examples": ["/feedback The plan mode is great"],
    },
    "reload": {
        "title": "Reload Config",
        "syntax": "/reload",
        "summary": "Reload configuration without exiting.",
        "description": "Re-resolves settings and re-applies the active model.",
        "examples": ["/reload"],
    },
    "debug": {
        "title": "Debug Context",
        "syntax": "/debug",
        "summary": "Show message/token/checkpoint counts and recent history.",
        "description": "Session message counts, tokens, turns, checkpoints and last messages.",
        "examples": ["/debug"],
    },
    "usage": {
        "title": "API Usage",
        "syntax": "/usage (aliases: /status, /quota)",
        "summary": "Show token consumption against the context window.",
        "description": "Local token accounting and estimated cost. This command does not retrieve provider account quota.",
        "examples": ["/usage"],
    },
    "task": {
        "title": "Task Browser",
        "syntax": "/task",
        "summary": "Open the interactive background-task browser.",
        "description": "Inspect foreground work, owned jobs, and subagents. Type to search; arrows select; Enter opens; Tab switches details; Esc returns. A selected job offers full output and explicit cancellation. Below 80 columns, one pane is shown.",
        "examples": ["/task"],
    },
    "agent": {
        "title": "Agent Role & Persona",
        "syntax": "/agent or /role [role_name]",
        "summary": "Inspect available agent roles or switch the active agent role in current session.",
        "description": (
            "Switch the specialized agent persona and tool capabilities.\n"
            "• /agent            — Open interactive agent role selector\n"
            "• /agent <role>     — Switch session to specified role (e.g. architect, code-reviewer)\n"
            "• /agent roles      — List all bundled and discovered markdown agent specifications"
        ),
        "examples": ["/agent", "/agent architect", "/role code-reviewer", "/agent default"],
    },
    "upgrade": {
        "title": "Upgrade",
        "syntax": "/upgrade",
        "summary": "Upgrade coderai-agent via pip.",
        "description": "Prints install + verify commands and optionally runs the upgrade.",
        "examples": ["/upgrade"],
    },
    "hooks": {
        "title": "Hooks",
        "syntax": "/hooks",
        "summary": "Show configured hooks.",
        "description": (
            "Lists hook events and handler counts. Events: PreToolUse, PostToolUse, "
            "PostToolUseFailure, UserPromptSubmit, Stop, StopFailure, SessionStart, "
            "SessionEnd, SubagentStart, SubagentStop, PreCompact, PostCompact, Notification "
            "(plus legacy PreTurn/PostTurn/PreStep/PostStep/ToolError/StopCriteria/SubagentSpawn)."
        ),
        "examples": ["/hooks"],
    },
    "reset": {
        "title": "Reset Session",
        "syntax": "/reset",
        "summary": "Clear conversation context and reset session state.",
        "description": "Clears conversation history, drops active plan mode, and resets session file cache.",
        "examples": ["/reset"],
    },
    "yolo": {
        "title": "YOLO Mode",
        "syntax": "/yolo",
        "summary": "Toggle YOLO mode (auto-approve all actions without prompts).",
        "description": "Toggles execution mode to bypass confirmation prompts for shell commands and file mutations.",
        "examples": ["/yolo"],
    },
    "afk": {
        "title": "AFK Mode",
        "syntax": "/afk",
        "summary": "Toggle AFK mode (auto-dismiss questions & approvals with defaults).",
        "description": "Toggles AFK mode so long-running agent tasks continue without blocking on interactive prompts.",
        "examples": ["/afk"],
    },
    "add-dir": {
        "title": "Add Workspace Directory",
        "syntax": "/add-dir <directory_path> (alias: /add_dir)",
        "summary": "Add an additional directory to the active workspace boundaries.",
        "description": "Expands workspace scope to allow tools to read and edit files in the specified directory.",
        "examples": ["/add-dir ../shared-lib"],
    },
    "context": {
        "title": "Context Utilization",
        "syntax": "/context",
        "summary": "Inspect live context window utilization and token allocation.",
        "description": "Shows breakdown of tokens across system prompt, conversation history, and tool outputs.",
        "examples": ["/context"],
    },
    "import": {
        "title": "Import Context",
        "syntax": "/import <file_or_session>",
        "summary": "Import context from a file into active session.",
        "description": "Reads contents of an external file and inserts it into current session context.",
        "examples": ["/import ./notes.txt"],
    },
    "web-vis": {
        "title": "Web Visualizer (Deprecated)",
        "syntax": "/web-vis (aliases: /web, /vis, /browser)",
        "summary": "Notice regarding removal of web frontend.",
        "description": "CoderAI is strictly a terminal CLI and daemon application. Web UI and browser viewers have been removed.",
        "examples": ["/web-vis"],
    },
}

# The canonical catalog drives help, aliases, and completion. Contextual
# documentation adds syntax/examples, but cannot hide catalog commands.
COMMAND_HELP_DETAILS.update(
    {
        "stop": {
            "syntax": "/stop",
            "description": "Interrupt owned foreground work and pause the queue. /queue run resumes queued prompts.",
        },
        "steer": {
            "syntax": "/steer <text>",
            "description": "Inject text into the running agent turn. Ctrl-S does the same from the composer. When idle, it submits normally. Shell and utility commands cannot accept steering.",
        },
        "queue": {
            "syntax": "/queue [list|edit <n>|remove <n>|move <from> <to>|clear|run]",
            "description": "Enter during generation prepares and queues a follow-up. Indices start at 1. Queued turns run sequentially; cancellation pauses them until /queue run or a new submission.",
        },
        "retry": {
            "syntax": "/retry",
            "description": "Resubmit the last failed or interrupted input with its images and skills. The current workspace is used; partial changes are retained.",
        },
        "output": {
            "syntax": "/output [message-id|tool-id|search]",
            "description": "Inspect complete tool results. Ctrl-E opens the browser while preserving the draft. Ctrl-F searches full output; Esc returns to the browser, then to the composer.",
        },
        "activity": {
            "syntax": "/activity",
            "description": "Show foreground status, pending interactions, owned jobs, and subagents. Enter opens details and actions. Background execution continues while browsing.",
        },
        "attach": {
            "syntax": "/attach <quoted-file-path>",
            "description": "Resolve a file and add its mention to the composer. Duplicate basenames require an explicit choice. Line ranges use a trailing :start-end.",
        },
        "attachments": {
            "syntax": "/attachments [edit|remove <n>|clear]",
            "description": "Ctrl-T opens the tray from the current draft. Review paths, ranges, sizes, and image dimensions; selection removes that attachment while retaining the rest of the draft. Indices start at 1; images precede files.",
        },
        "display": {
            "syntax": "/display [compact|verbose|accessible|animated]",
            "description": "Persist output detail and accessibility preferences. Accessible mode uses static text, numbered choices, ASCII markers, and no animated preview. NO_COLOR removes colors without disabling controls.",
        },
    }
)
for name, details in COMMAND_HELP_DETAILS.items():
    command = COMMAND_CATALOG.get(name)
    if command:
        details.setdefault("title", command.name.capitalize())
        details.setdefault("summary", command.summary)
        details.setdefault("examples", [f"/{name}"])


def _catalog_help_groups() -> list[tuple[str, list[tuple[str, str, str]]]]:
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for command in _COMMANDS:
        syntax = COMMAND_HELP_DETAILS.get(command.name, {}).get("syntax", f"/{command.name}")
        suffix = str(syntax).removeprefix(f"/{command.name}").strip()
        if suffix.startswith("(alias"):
            suffix = ""
        groups.setdefault(command.category, []).append(
            (str(command.display_name), suffix, command.summary)
        )
    groups.setdefault("Models & Reasoning", []).extend(
        [
            ("/skill:<name>", "", "Load a discovered skill"),
            ("/flow:<name>", "", "Run a flow skill under foreground cancellation ownership"),
        ]
    )
    return list(groups.items())


HELP_GROUPS = _catalog_help_groups()


def render_help(cmd_name: str | None = None, console: Any | None = None) -> None:
    """Display clean slash command cheatsheet or contextual command help."""
    # Contextual help for specific command
    if cmd_name:
        _render_contextual_help(cmd_name, console)
        return

    # Full help overview
    if console is not None and _RICH and Table is not None:
        _render_rich_overview(console)
    else:
        _render_plain_overview()


def _render_contextual_help(cmd_name: str, console: Any | None) -> None:
    key = cmd_name.strip().lstrip("/").lower()
    details = COMMAND_HELP_DETAILS.get(key)
    command = resolve_command(key)
    canonical_key = command.name if command else key
    if not details and canonical_key:
        details = COMMAND_HELP_DETAILS.get(canonical_key)

    if not details and command:
        details = {
            "title": command.name.capitalize(),
            "syntax": str(command.display_name),
            "summary": command.summary or command.description,
            "description": command.description or command.summary,
            "examples": [f"/{command.name}"],
        }

    if details:
        display_key = canonical_key or key
        if console is not None and _RICH and Panel is not None:
            summary_esc = escape(str(details.get("summary", "")))
            syntax_esc = escape(str(details.get("syntax", "")))
            desc_esc = escape(str(details.get("description", "")))

            body = (
                f"[bold white]{summary_esc}[/]\n\n"
                f"[bold cyan]Syntax:[/] [bold yellow]{syntax_esc}[/]\n\n"
                f"[bold white]Details:[/]\n{desc_esc}\n"
            )
            if details.get("examples"):
                body += "\n[bold magenta]Examples:[/]\n"
                for ex in details["examples"]:
                    body += f"  [dim cyan]•[/] [bold green]{escape(str(ex))}[/]\n"

            panel = Panel(
                body.strip(),
                title=f"[bold cyan]CoderAI Help:[/] [bold yellow]/{display_key}[/]",
                border_style="cyan",
                padding=(0, 1),
            )
            console.print()
            console.print(panel)
            console.print()
        else:
            print(f"\n--- CoderAI Command Help: /{display_key} ---")
            print(f"Summary: {details['summary']}")
            print(f"Syntax:  {details['syntax']}\n")
            print("Details:")
            print(details["description"])
            if details.get("examples"):
                print("\nExamples:")
                for ex in details["examples"]:
                    print(f"  • {ex}")
            print()
    else:
        all_keys = list(COMMAND_HELP_DETAILS.keys()) + list(COMMAND_ALIASES.keys())
        matches = difflib.get_close_matches(key, all_keys, n=1, cutoff=0.5)
        suggestion = f" (did you mean '/{matches[0]}'?)" if matches else ""

        if console is not None and _RICH:
            console.print(
                f"[dim yellow]No specific help found for '{cmd_name}'{suggestion}. Showing full help menu:[/]\n"
            )
            _render_rich_overview(console)
        else:
            print(f"No specific help found for '{cmd_name}'{suggestion}. Showing full help menu:\n")
            _render_plain_overview()


def _help_command_column_width(term_cols: int = 80, *, cap: bool = True) -> int:
    """Width of the command column, wide enough for the longest entry.

    A hardcoded 30/34-column pad clipped long commands on narrow terminals
    and left a ragged description column. Plain text keeps the full width so
    every description starts in the same column; the rich view caps it so the
    description column still fits.
    """
    longest = 0
    for _, commands in HELP_GROUPS:
        for cmd, arg, _desc in commands:
            label = f"{cmd} {arg}".strip() if arg else cmd
            longest = max(longest, len(label))
    longest = max(longest, 1)
    if not cap:
        return longest
    return max(18, min(longest, max(18, term_cols // 2)))


def _render_rich_overview(console: Any) -> None:
    """Render clean, simple, and modern grouped cheatsheet using borderless grid."""
    import shutil as _shutil

    width = getattr(console, "width", None) or 80
    try:
        if not width or width < 20:
            width = _shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        width = 80
    cmd_width = _help_command_column_width(width)

    console.print()
    console.print("[bold cyan]CoderAI[/] [dim]· Interactive Slash Commands[/]")
    console.print()

    for group_name, commands in HELP_GROUPS:
        console.print(f"[bold cyan]{group_name}[/]")
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(style="bold green", width=cmd_width, overflow="ellipsis", no_wrap=True)
        grid.add_column(style="white", overflow="fold", ratio=1)

        for cmd, arg, desc in commands:
            cmd_text = Text()
            parts = [p.strip() for p in cmd.split(",")]
            primary = parts[0]
            cmd_text.append(primary, style="bold green")
            if len(parts) > 1:
                cmd_text.append(", " + ", ".join(parts[1:]), style="dim green")
            if arg:
                cmd_text.append(f" {arg}", style="dim yellow")

            grid.add_row("", cmd_text, desc)

        console.print(grid)
        console.print()

    console.print(
        "[dim]Shortcuts: [bold cyan]Tab[/] complete · [bold cyan]Ctrl-R[/] history search · [bold cyan]Ctrl-C[/] interrupt · [bold cyan]Ctrl-D[/] exit · [bold cyan]@file.py[:10-30][/] file context[/]"
    )
    console.print(
        "[dim]Type [bold cyan]/help <command>[/] for detailed syntax and examples (e.g. [bold cyan]/help goal[/], [bold cyan]/help plan[/]).[/]\n"
    )


def _render_plain_overview() -> None:
    """Render clean plain-text overview fallback."""
    cmd_width = _help_command_column_width(80, cap=False)
    print("\n--- CoderAI Slash Commands ---")
    for group_name, commands in HELP_GROUPS:
        print(f"\n{group_name}:")
        for cmd, arg, desc in commands:
            full_cmd = f"{cmd} {arg}".strip() if arg else cmd
            print(f"  {full_cmd:<{cmd_width}}  {desc}")

    print(
        "\nShortcuts: Tab complete · Ctrl-R history search · Ctrl-S steer · Ctrl-T attachments · Ctrl-C interrupt · Ctrl-D exit · @file context"
    )
    print("Type /help <command> for detailed syntax and examples (e.g. /help goal, /help plan).\n")


import os


import pathlib


import sys


import webbrowser


try:
    from coderai._version import __version__ as _CODERAI_VERSION
except Exception:
    _CODERAI_VERSION = "0.0.0"


_FEEDBACK_URL = "https://github.com/adityaanilraut/CoderAI/issues/new"


_CODERAI_HOOK_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "StopFailure",
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "Notification",
)


def cmd_version(console: Any = None) -> str:
    """Print CLI version. Returns the version string."""
    text = f"coderai, version {_CODERAI_VERSION}"
    if console is not None:
        try:
            console.print(f"[bold cyan]{text}[/]")
        except Exception:
            print(text)
    else:
        print(text)
    return _CODERAI_VERSION


def cmd_changelog(console: Any = None, limit: int = 10) -> str:
    """Print recent CHANGELOG entries."""
    root = pathlib.Path(__file__).resolve().parents[2]
    changelog = root / "CHANGELOG.md"
    body = ""
    if changelog.exists():
        try:
            lines = changelog.read_text(encoding="utf-8", errors="replace").splitlines()
            # Keep header + first `limit` version sections.
            kept: list[str] = []
            sections = 0
            for line in lines:
                if line.startswith("## ") and kept:
                    sections += 1
                    if sections > limit:
                        break
                kept.append(line)
            body = "\n".join(kept).strip()
        except Exception:
            body = ""
    if not body:
        body = f"# Changelog\n\n## {_CODERAI_VERSION}\n\n- No changelog entries found."
    if console is not None:
        try:
            from rich.markdown import Markdown

            console.print(Markdown(body[:6000]))
        except Exception:
            print(body[:6000])
    else:
        print(body[:6000])
    return body


def cmd_feedback(console: Any = None, text: str = "") -> str:
    """Collect feedback; try GitHub issue URL, fall back to printing it.

    Prompt for feedback, POST, fallback to opening GitHub Issues.
    Offline-safe: opens/prints the issue URL with the body prefilled.
    """
    feedback = (text or "").strip()
    if not feedback and sys.stdin.isatty():
        try:
            feedback = input("Feedback (Enter to open GitHub Issues): ").strip()
        except (EOFError, KeyboardInterrupt):
            feedback = ""
    import urllib.parse

    url = _FEEDBACK_URL
    if feedback:
        url += "?body=" + urllib.parse.quote(feedback[:2000])
    try:
        if sys.stdin.isatty():
            webbrowser.open(url)
            opened = True
        else:
            opened = False
    except Exception:
        opened = False
    msg = f"Feedback URL: {url}" if not opened else "Opened feedback page in browser."
    if console is not None:
        try:
            console.print(f"[bold cyan]{msg}[/]")
        except Exception:
            print(msg)
    else:
        print(msg)
    return url


def cmd_reload(mgr: Any, console: Any = None) -> bool:
    """Reload resolved settings without exiting."""
    try:
        from coderai.config import resolve_current_settings

        settings = resolve_current_settings(getattr(mgr, "project_root", "."))
        model = settings.get("model") or settings.get("active_model")
        if model:
            if hasattr(mgr, "set_model"):
                try:
                    mgr.set_model(str(model))
                except Exception:
                    pass
            if hasattr(mgr, "set_active_model"):
                try:
                    mgr.set_active_model(str(model))
                except Exception:
                    pass
        msg = "Configuration reloaded."
        if console is not None:
            try:
                console.print(f"[bold green]✓ {msg}[/]")
            except Exception:
                print(msg)
        else:
            print(msg)
        return True
    except Exception as e:
        err = f"Reload failed: {e}"
        if console is not None:
            try:
                console.print(f"[bold red]{err}[/]")
            except Exception:
                print(err)
        else:
            print(err)
        return False


def cmd_title(mgr: Any, session_id: str | None, arg: str = "", console: Any = None) -> str | None:
    """View or set the session title (max 200 chars)."""
    entry = mgr.get_session(session_id) if session_id else None
    if entry is None:
        msg = "No active session."
        if console is not None:
            try:
                # UI-A14: session titles are user/model-written; never markup.
                console.print(f"[bold cyan]{escape(msg)}[/]")
            except Exception:
                print(msg)
        else:
            print(msg)
        return None
    text = (arg or "").strip()
    if text:
        text = text[:200]
        try:
            if hasattr(mgr, "rename_session"):
                mgr.rename_session(session_id, text)
            else:
                entry.summary = text
            # Manual set locks auto-generation.
            setattr(entry, "title_locked", True)
        except Exception:
            pass
        msg = f"Title set: {text}"
    else:
        current = getattr(entry, "summary", "") or "(untitled)"
        msg = f"Title: {current}"
        text = str(current)
    if console is not None:
        try:
            console.print(f"[bold cyan]{msg}[/]")
        except Exception:
            print(msg)
    else:
        print(msg)
    return text


def cmd_hooks(console: Any = None, project_root: str = ".") -> dict[str, Any]:
    """Show configured hooks (event types + counts)."""
    from coderai.hooks import load_hook_config, normalize_hook_point

    cfg = load_hook_config(project_root)
    events: dict[str, int] = {}
    try:
        raw = cfg.get("hooks", {}) if isinstance(cfg, dict) else {}
        if not raw and isinstance(cfg, dict):
            raw = cfg
        for event, handlers in (raw or {}).items():
            try:
                canonical = normalize_hook_point(str(event))
            except Exception:
                canonical = str(event)
            count = len(handlers) if isinstance(handlers, list) else 1
            # Count nested {"hooks": [...]} entries individually.
            total = 0
            if isinstance(handlers, list):
                for entry in handlers:
                    if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                        total += len(entry["hooks"])
                    else:
                        total += 1
            else:
                total = count
            events[canonical] = events.get(canonical, 0) + total
    except Exception:
        pass
    if console is not None:
        try:
            from rich.table import Table

            if events:
                t = Table(title="Configured Hooks", border_style="cyan")
                t.add_column("Event", style="bold cyan")
                t.add_column("Handlers", style="white")
                for ev in sorted(
                    events,
                    key=lambda e: (
                        _CODERAI_HOOK_EVENTS.index(e) if e in _CODERAI_HOOK_EVENTS else 99,
                        e,
                    ),
                ):
                    t.add_row(ev, str(events[ev]))
                console.print(t)
                missing = [e for e in _CODERAI_HOOK_EVENTS if e not in events]
                if missing:
                    console.print(f"[dim]No handlers for: {', '.join(missing)}[/]")
            else:
                console.print(
                    "[dim]No hooks configured. Events: " + ", ".join(_CODERAI_HOOK_EVENTS) + "[/]"
                )
        except Exception:
            print(events or "No hooks configured.")
    else:
        print(events or "No hooks configured.")
    return {"events": events}


def cmd_upgrade(console: Any = None) -> str:
    """Run upgrade check and package upgrade (delegates to coderai.ui.shell.update)."""
    from coderai.ui.shell.update import UPGRADE_COMMAND, do_update

    try:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(do_update(print=True))
        except RuntimeError:
            asyncio.run(do_update(print=True))
    except Exception as e:
        if console is not None:
            console.print(f"[red]Upgrade check failed: {e}[/red]")
        else:
            print(f"Upgrade check failed: {e}")
    return UPGRADE_COMMAND


def cmd_login(
    console: Any = None,
    project_root: str = ".",
    mgr: Any = None,
    initial: str | None = None,
) -> int:
    """Platform select → OAuth/API key → model → save + reload."""
    from coderai.ui.shell.setup import run_setup_wizard

    run_setup_wizard(
        console,
        project_root=project_root,
        mgr=mgr,
        initial_subcommand=initial,
    )
    if mgr is not None:
        cmd_reload(mgr, console)
    return 0


def cmd_logout(console: Any = None, project_root: str = ".", mgr: Any = None) -> int:
    """Clear stored credentials from the raw settings files (UI-A6)."""
    try:
        from coderai.config import (
            read_project_settings,
            read_settings,
            write_project_settings,
            write_settings,
        )

        removed: list[str] = []

        def _scrub(data: dict | None, scope: str) -> dict | None:
            if not isinstance(data, dict):
                return None
            changed = False
            # Top-level apiKey (legacy) and env-exported keys.
            for key in ("apiKey",):
                if data.pop(key, None) is not None:
                    removed.append(f"{scope}:{key}")
                    changed = True
            env = data.get("env")
            if isinstance(env, dict):
                for name in [k for k in env if "KEY" in str(k).upper()]:
                    env.pop(name, None)
                    removed.append(f"{scope}:env.{name}")
                    changed = True
            providers = data.get("providers")
            if isinstance(providers, dict):
                for pname, prov in providers.items():
                    if isinstance(prov, dict) and prov.pop("api_key", None) is not None:
                        removed.append(f"{scope}:providers.{pname}.api_key")
                        changed = True
            return data if changed else data

        user = read_settings()
        if isinstance(user, dict):
            _scrub(user, "user")
            try:
                write_settings(user)
            except Exception:
                pass
        project = read_project_settings(project_root)
        if isinstance(project, dict):
            _scrub(project, "project")
            try:
                write_project_settings(project, project_root)
            except Exception:
                pass
        # Also clear env-exported keys for this process.
        for name in list(os.environ.keys()):
            if name.startswith("CODERAI_") and "KEY" in name:
                del os.environ[name]
        # Drop OAuth tokens + the managed provider too.
        try:
            from coderai.auth.oauth import clear_login_config, logout_all

            removed_tokens = logout_all()
            cleared_provider = clear_login_config()
            removed.extend(f"oauth:{t}" for t in removed_tokens)
        except Exception:
            cleared_provider = False
        if removed:
            msg = f"Logged out (removed: {', '.join(removed)})."
        else:
            msg = "Logged out (no stored credentials found)."
        if cleared_provider:
            msg += " Managed OAuth provider removed."
        if console is not None:
            try:
                console.print(f"[bold green]✓ {msg}[/]")
            except Exception:
                print(msg)
        else:
            print(msg)
        if mgr is not None:
            cmd_reload(mgr, console)
        return 0
    except Exception as e:
        print(f"Logout failed: {e}")
        return 1
