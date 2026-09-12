# Ported from coderai/cli/commands.py - kimi structure (ui/shell/slash.py).
"""Canonical slash-command catalog shared by help, completion, and dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from coderai.ui.shell.dispatch import (
    SlashAction,
    ShellContext,
    dispatch_slash_command,
    registry,
    shell_mode_registry,
)


@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    category: str
    aliases: tuple[str, ...] = ()
    subcommands: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        aliases = ", ".join(f"/{alias}" for alias in self.aliases)
        return f"/{self.name}" + (f", {aliases}" if aliases else "")


_COMMANDS = (
    SlashCommand("new", "Start a fresh session", "Session Management"),
    SlashCommand("init", "Initialize or update AGENTS.md guidelines", "Session Management"),
    SlashCommand("sessions", "Browse, resume, delete, or fork sessions", "Session Management"),
    SlashCommand("resume", "Resume a saved session by ID", "Session Management"),
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
    SlashCommand("usage", "Show API usage / quota", "Help & Info", ("status", "quota")),
    SlashCommand(
        "plan",
        "Toggle or apply Plan Mode",
        "Planning & Safety",
        subcommands=("on", "off", "view", "clear", "apply", "reset"),
    ),
    SlashCommand("undo", "Revert to a previous checkpoint", "Planning & Safety"),
    SlashCommand("diff", "Show the current unified diff", "Planning & Safety"),
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
    SlashCommand("model", "Select or switch the active model", "Models & Reasoning"),
    SlashCommand(
        "effort",
        "Select reasoning effort",
        "Models & Reasoning",
        ("reasoning",),
        ("max", "high", "medium", "low", "off"),
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
        "Inspect subagent runs",
        "Diagnostics",
        ("subagents", "subagent"),
        ("list", "tree", "report", "send"),
    ),
    SlashCommand("teams", "Inspect active agent teams", "Diagnostics"),
    SlashCommand(
        "mcp",
        "Inspect MCP servers, tools, prompts, and resources",
        "Tools & Analytics",
        subcommands=("reconnect", "prompts", "resources"),
    ),
    SlashCommand("tokens", "Show token usage", "Tools & Analytics", ("cost",)),
    SlashCommand(
        "compact", "Compress conversation context", "Tools & Analytics", subcommands=("keep",)
    ),
    SlashCommand("history", "Show the session timeline", "Tools & Analytics"),
    SlashCommand("import", "Import context from file or session", "Tools & Analytics"),
    SlashCommand("config", "Show resolved configuration", "Tools & Analytics", ("settings",)),
    SlashCommand(
        "permission",
        "Show or set the permission preset",
        "Tools & Analytics",
        ("permissions",),
        (
            "read-only",
            "workspace-write",
            "danger-full-access",
            "local-network-read",
            "unrestricted-read",
        ),
    ),
    SlashCommand(
        "goal",
        "List or update session goals",
        "Tools & Analytics",
        subcommands=("list", "add", "done", "cancel", "start"),
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
        ("role",),
        ("roles", "switch", "list"),
    ),
    SlashCommand("upgrade", "Check for and install CoderAI updates", "Utilities"),
    SlashCommand("hooks", "Show configured hooks", "Utilities"),
    SlashCommand("btw", "Side question (BTW modal)", "Utilities"),
    SlashCommand("help", "Show command help", "Utilities", ("?", "h")),
    SlashCommand("exit", "Exit CoderAI", "Utilities", ("quit",)),
)

COMMAND_CATALOG = {command.name: command for command in _COMMANDS}
COMMAND_ALIASES = {alias: command.name for command in _COMMANDS for alias in command.aliases}


def resolve_command(name: str) -> SlashCommand | None:
    """Resolve a command or alias without its leading slash."""
    key = name.strip().lower().lstrip("/")
    return COMMAND_CATALOG.get(COMMAND_ALIASES.get(key, key))


def parse_slash_command(raw: str) -> tuple[str, str]:
    """Return a canonical slash command and its unmodified argument text."""
    command_text, _, argument = raw.strip().partition(" ")
    resolved = resolve_command(command_text)
    canonical = resolved.name if resolved else command_text.lower().lstrip("/")
    return f"/{canonical}", argument.strip()


def completion_entries() -> list[tuple[str, str]]:
    """Return canonical commands and aliases for readline completion."""
    entries: list[tuple[str, str]] = []
    for command in _COMMANDS:
        entries.append((f"/{command.name}", command.summary))
        entries.extend((f"/{alias}", f"{command.summary} (alias)") for alias in command.aliases)
    return entries


# --- from coderai/cli/help.py ---
"""Clean, modern, and simple slash-command help system for CoderAI CLI."""


import difflib
from typing import Any


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
        "syntax": "/plan [on|off|apply|reset]",
        "summary": "Toggle or manage Plan Mode (strict read-only safety boundary).",
        "description": (
            "Plan Mode enforces a strict read-only boundary where the agent analyzes the codebase "
            "and formulates an architectural implementation plan without mutating files or running destructive commands.\n"
            "• /plan        — Toggle Plan Mode on/off\n"
            "• /plan on     — Turn on Plan Mode\n"
            "• /plan off    — Turn off Plan Mode\n"
            "• /plan apply  — Approve plan, turn off Plan Mode, and begin implementation\n"
            "• /plan reset  — Reset plan mode state to default"
        ),
        "examples": ["/plan", "/plan on", "/plan apply", "/plan reset"],
    },
    "goal": {
        "title": "Session Goals",
        "syntax": "/goal [list|add <title>|done <id>|cancel <id>|start <id>]",
        "summary": "Manage session goals and milestone tracking.",
        "description": (
            "Track high-level goals and progress for the current session.\n"
            "• /goal                      — List all goals for active session\n"
            "• /goal add <title>          — Add a new goal\n"
            "• /goal done <id>            — Mark goal as completed\n"
            "• /goal cancel <id>          — Cancel goal\n"
            "• /goal start <id>           — Mark goal in-progress"
        ),
        "examples": [
            "/goal",
            "/goal add Refactor database migration logic",
            "/goal done 1",
            "/goal cancel 2",
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
            "• local-network-read   — Allow local network read operations\n"
            "• unrestricted-read    — Unrestricted read across filesystem"
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
        "syntax": "/agents or /subagents [list|roles|tree|report <id>|send <id> <msg>]",
        "summary": "Inspect hierarchical subagent runs, discovered roles, and communicate with child agents.",
        "description": (
            "Monitor and interact with delegated subagent tasks and roles.\n"
            "• /agents                   — List running and completed subagents\n"
            "• /agents roles             — List bundled and discovered agent roles (.coderai/agents/*.md)\n"
            "• /agents tree              — View hierarchical tree of subagent delegation\n"
            "• /agents report <id>       — Display final report or findings from subagent\n"
            "• /agents send <id> <msg>   — Send instruction message into subagent inbox"
        ),
        "examples": [
            "/agents",
            "/agents roles",
            "/agents tree",
            "/agents report ag_1",
            "/agents send ag_1 Please check module B",
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
            "You can also mention image files directly in your prompts."
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
            "When you save and close the editor, the content is automatically submitted as your prompt."
        ),
        "examples": ["/editor", "/edit"],
    },
    "paste": {
        "title": "Multiline Paste Mode",
        "syntax": "/paste",
        "summary": "Enter multiline paste mode for long code snippets or text blocks.",
        "description": (
            "Enters dedicated multiline capture mode. Paste text freely and type ':::' on a new line "
            "or press Ctrl-D to complete input.\n"
            'Tip: You can also wrap prompts directly in """ triple quotes """ or ``` code fences.'
        ),
        "examples": ["/paste"],
    },
    "model": {
        "title": "Model Selection",
        "syntax": "/model [name]",
        "summary": "Select or switch active LLM model.",
        "description": "Opens interactive curated model menu or switches to specified model identifier directly.",
        "examples": [
            "/model",
            "/model deepseek-v4-pro",
            "/model gemini-2.5-pro",
            "/model claude-3-7-sonnet",
        ],
    },
    "effort": {
        "title": "Reasoning Effort Selection",
        "syntax": "/effort [off|low|medium|high|max]",
        "summary": "Select or switch reasoning effort level (thinking token budget).",
        "description": "Opens interactive reasoning effort menu or sets effort tier (off, low, medium, high, max).",
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
        "syntax": "/reasoning [off|low|medium|high|max]",
        "summary": "Alias for /effort.",
        "description": "Select or switch reasoning effort level.",
        "examples": ["/reasoning", "/reasoning max"],
    },
    "sessions": {
        "title": "Saved Sessions Browser",
        "syntax": "/sessions [search_query]",
        "summary": "Paginated interactive session browser with search filter, resume, delete, and fork.",
        "description": (
            "Browse saved workspace sessions.\n"
            "Interactive controls:\n"
            "• <num>       — Resume session\n"
            "• d <num>     — Delete session\n"
            "• f <num>     — Fork session\n"
            "• s <query>   — Filter sessions\n"
            "• n / p       — Next / previous page"
        ),
        "examples": ["/sessions", "/sessions auth", "/sessions refactor"],
    },
    "undo": {
        "title": "Undo & Checkpoint Rollback",
        "syntax": "/undo",
        "summary": "Interactive turn and checkpoint rollback (revert files, conversation, or both).",
        "description": "Revert code and conversation history to any previous turn checkpoint.",
        "examples": ["/undo"],
    },
    "diff": {
        "title": "Diff Preview",
        "syntax": "/diff",
        "summary": "Display syntax-highlighted unified diff of changes made during the session.",
        "description": "Renders git diff of workspace modifications since session start.",
        "examples": ["/diff"],
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
        "description": "Displays chronological timeline of user prompts, tool executions, and tokens.",
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
        "description": (
            "• Ctrl-C       — Interrupt active generation or cancel current input line\n"
            "• Ctrl-D       — Exit CoderAI REPL session gracefully\n"
            "• Ctrl-R       — Reverse history search (interactive readline history search)\n"
            "• Ctrl-L       — Clear terminal screen and redraw status bar\n"
            "• Tab          — Autocomplete slash commands, models, sub-arguments, and @file paths\n"
            "• Shift-Tab    — Toggle Plan Mode / Build Mode\n"
            "• @<filename>  — Mention workspace file with optional line slice (@file.py:10-40)\n"
            "• \"\"\" or '''   — Multiline block input delimiter\n"
            "• \\ at end     — Line continuation"
        ),
        "examples": ["/help shortcuts"],
    },
    "theme": {
        "title": "Theme Switch",
        "syntax": "/theme [dark|light]",
        "summary": "Switch terminal theme (dark/light) for diff background colors.",
        "description": "Toggles diff background colors via theme.set_active_theme. • /theme dark — dark diff bg • /theme light — light diff bg • /theme — show current",
        "examples": ["/theme", "/theme dark", "/theme light"],
    },
    "btw": {
        "title": "Side Question (BTW)",
        "syntax": "/btw <question>",
        "summary": "Ask a side question without interrupting current turn (BTW modal).",
        "description": "Routes to PromptPlaceholderManager + BtwPanel (modal_priority=5). While streaming, queues as BTW modal not ❯ queue. Resolves via TextPart/ImageURLPart wrap_media_part.",
        "examples": ["/btw What does this error mean?", "/btw Summarize the file"],
    },
    "login": {
        "title": "Log In",
        "syntax": "/login",
        "summary": "Log in / configure API platform (OAuth or API key), then pick model.",
        "description": "Platform select, API key entry, model selection, save + reload. Mirrors Kimi /login.",
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
        "description": "Progress-bar usage panel using local token accounting.",
        "examples": ["/usage"],
    },
    "task": {
        "title": "Task Browser",
        "syntax": "/task",
        "summary": "Open the interactive background-task browser.",
        "description": "Three-column TUI: list | detail | output preview. Enter/O output, S stop, Tab filter, R refresh, Q exit.",
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
}

# Grouped command definitions for clean, categorized presentation
HELP_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "Session Management",
        [
            ("/new", "", "Start a fresh session in the workspace"),
            ("/sessions", "[query]", "Interactive session browser (resume, delete, fork, search)"),
            ("/resume", "<id>", "Resume a saved session by ID directly"),
            ("/fork", "[id]", "Fork current or target session into new branch"),
            ("/delete, /rm", "<id>", "Delete a saved session from workspace"),
            ("/rename", "[title]", "Rename active or specified session summary"),
            ("/export", "[file]", "Export session history to Markdown or JSON"),
            ("/init", "", "Initialize or update AGENTS.md contributor guidelines"),
            ("/btw", "<question>", "Side question (BTW modal, not queued)"),
        ],
    ),
    (
        "Planning & Safety",
        [
            ("/plan", "[on|off|apply]", "Toggle Plan Mode (strict read-only safety boundary)"),
            ("/undo", "", "Interactive turn & checkpoint rollback (code, conversation, or both)"),
            ("/diff", "", "Show syntax-highlighted diff of changes"),
            ("/continue", "", "Continue bounded multi-step agent execution"),
        ],
    ),
    (
        "Models & Reasoning",
        [
            ("/model", "[name]", "Interactive model selector or switch directly"),
            ("/effort", "[level]", "Interactive reasoning effort selector (low..max, off)"),
            ("/thinking, /raw", "", "Toggle full reasoning trace or summary (lite/normal)"),
            ("/setup, /keys", "", "Configure API keys, providers, local endpoints & default model"),
            ("/skills", "", "Explore active and discovered workspace skills"),
            ("/skill", "<name>", "Load a skill into the current session"),
        ],
    ),
    (
        "Diagnostics & Tools",
        [
            ("/doctor", "", "Run comprehensive system health checks & connectivity"),
            (
                "/permission, /permissions",
                "",
                "Show or set permission preset (read-only, workspace-write, danger-full-access)",
            ),
            (
                "/mcp",
                "[subcommand]",
                "Inspect MCP servers/tools, /mcp prompts, /mcp resources, /mcp reconnect",
            ),
            ("/goal", "[add|done] ...", "List or update session goals"),
            ("/jobs, /job", "[subcmd]", "Inspect and manage background bash jobs"),
            ("/schedule", "[subcmd]", "Manage session-scoped reminders and timers"),
            ("/agents, /subagents", "", "Inspect subagent hierarchy, tree, and reports"),
            ("/tokens, /cost", "", "View detailed token usage and context analytics"),
            ("/compact", "", "Compress history to free up active context tokens"),
            ("/history", "", "View turn-by-turn conversation timeline"),
            ("/config, /settings", "", "Inspect resolved workspace & user settings"),
            ("/teams", "", "Inspect active agent team members and status"),
        ],
    ),
    (
        "Input & Media",
        [
            ("/image", "<path> [prompt]", "Attach image file for multimodal vision analysis"),
            ("/editor, /edit", "", "Open external $EDITOR (nano, vim, vi) to compose prompt"),
            ("/paste", "", "Enter multiline paste mode until ':::' or Ctrl-D"),
        ],
    ),
    (
        "Utilities & Controls",
        [
            ("/clear", "", "Clear terminal screen and redraw status"),
            ("/help, /?", "[command]", "Show command help menu or detailed contextual help"),
            ("/theme", "[dark|light]", "Switch diff theme (dark/light)"),
            ("/task", "", "Interactive background-task browser (list/detail/output)"),
            ("/upgrade", "", "Upgrade coderai-agent via pip"),
            ("/exit, /quit", "", "Exit CoderAI session with summary card"),
        ],
    ),
    (
        "Help & Info",
        [
            ("/version", "", "Show CLI version"),
            ("/changelog", "", "Show recent changelog (/release-notes)"),
            ("/feedback", "[text]", "Submit feedback (falls back to GitHub Issues)"),
            ("/reload", "", "Reload configuration without exiting"),
            ("/debug", "", "Context debug info (msgs/tokens/checkpoints)"),
            ("/usage", "", "API usage / quota (/status, /quota)"),
            ("/hooks", "", "Show configured hooks"),
        ],
    ),
    (
        "Account",
        [
            ("/login", "", "Log in / configure API platform (OAuth or key)"),
            ("/logout", "", "Log out: clear stored credentials"),
            ("/setup, /keys", "", "Configure API keys, providers, local endpoints & default model"),
        ],
    ),
]


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
    command = resolve_command(key)
    canonical_key = command.name if command else key
    details = COMMAND_HELP_DETAILS.get(canonical_key)

    if details:
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
                title=f"[bold cyan]CoderAI Help:[/] [bold yellow]/{canonical_key}[/]",
                border_style="bright_blue",
                padding=(1, 2),
            )
            console.print()
            console.print(panel)
            console.print()
        else:
            print(f"\n--- CoderAI Command Help: /{canonical_key} ---")
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


def _render_rich_overview(console: Any) -> None:
    """Render clean, simple, and modern grouped cheatsheet using borderless grid."""
    console.print()
    console.print("[bold cyan]CoderAI[/] [dim]• Interactive Slash Commands[/]")
    console.print()

    for group_name, commands in HELP_GROUPS:
        console.print(f"[bold cyan]  {group_name}[/]")
        grid = Table.grid(padding=(0, 2))
        grid.add_column("Command", style="bold green", width=34)
        grid.add_column("Description", style="white")

        for cmd, arg, desc in commands:
            cmd_text = Text()
            cmd_text.append("    ")
            # Split comma separated aliases
            parts = [p.strip() for p in cmd.split(",")]
            primary = parts[0]
            cmd_text.append(primary, style="bold green")
            if len(parts) > 1:
                cmd_text.append(", " + ", ".join(parts[1:]), style="dim green")
            if arg:
                cmd_text.append(f" {arg}", style="dim yellow")

            grid.add_row(cmd_text, desc)

        console.print(grid)
        console.print()

    console.print(
        "[dim]  Shortcuts: [bold cyan]Tab[/] complete • [bold cyan]Ctrl-R[/] history search • [bold cyan]Ctrl-C[/] interrupt • [bold cyan]Ctrl-D[/] exit • [bold cyan]@file.py[:10-30][/] file context[/]"
    )
    console.print(
        "[dim]  Type [bold cyan]/help <command>[/] for detailed syntax and examples (e.g. [bold cyan]/help goal[/], [bold cyan]/help plan[/]).[/]\n"
    )


def _render_plain_overview() -> None:
    """Render clean plain-text overview fallback."""
    print("\n--- CoderAI Slash Commands ---")
    for group_name, commands in HELP_GROUPS:
        print(f"\n{group_name}:")
        for cmd, arg, desc in commands:
            full_cmd = f"{cmd} {arg}".strip() if arg else cmd
            print(f"  {full_cmd:<30} {desc}")

    print(
        "\nShortcuts: Tab complete | Ctrl-R history search | Ctrl-C interrupt | Ctrl-D exit | @file context"
    )
    print("Type /help <command> for detailed syntax and examples (e.g. /help goal, /help plan).\n")


# --- from coderai/cli/info_cmds.py (slash half) ---


import datetime as _dt


import os


import pathlib


import sys


import webbrowser


from typing import Any


try:
    from coderai._version import __version__ as _CODERAI_VERSION
except Exception:
    _CODERAI_VERSION = "0.0.0"


_FEEDBACK_URL = "https://github.com/adityaanilraut/CoderAI/issues/new"


_KIMI_HOOK_EVENTS = (
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
    """Print CLI version (Kimi ``/version`` parity). Returns the version string."""
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
    """Print recent CHANGELOG entries (Kimi ``/changelog`` parity)."""
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

    Kimi parity: prompt for feedback, POST, fallback to opening GitHub Issues.
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
        webbrowser.open(url)
        opened = True
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
    """Reload resolved settings without exiting (Kimi ``/reload`` parity)."""
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


def cmd_title(
    mgr: Any, session_id: str | None, arg: str = "", console: Any = None
) -> str | None:
    """View or set the session title (Kimi ``/title`` parity, max 200 chars)."""
    entry = mgr.get_session(session_id) if session_id else None
    if entry is None:
        msg = "No active session."
        if console is not None:
            try:
                console.print(f"[yellow]{msg}[/]")
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
            # Manual set locks auto-generation (Kimi parity).
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
    """Show configured hooks (Kimi ``/hooks`` parity: event types + counts)."""
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
                for ev in sorted(events, key=lambda e: (_KIMI_HOOK_EVENTS.index(e) if e in _KIMI_HOOK_EVENTS else 99, e)):
                    t.add_row(ev, str(events[ev]))
                console.print(t)
                missing = [e for e in _KIMI_HOOK_EVENTS if e not in events]
                if missing:
                    console.print(f"[dim]No handlers for: {', '.join(missing)}[/]")
            else:
                console.print(
                    "[dim]No hooks configured. Events: "
                    + ", ".join(_KIMI_HOOK_EVENTS)
                    + "[/]"
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
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(lambda: asyncio.run(do_update(print=True))).result()
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
    """Kimi ``/login`` parity: platform select → OAuth/API key → model → save + reload."""
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
    """Kimi ``/logout`` parity: clear stored credentials."""
    try:
        from coderai.config import (
            get_configured_provider_keys,
            resolve_current_settings,
            write_settings,
        )

        settings = resolve_current_settings(project_root) or {}
        providers = get_configured_provider_keys(project_root)
        # Strip API keys from user settings providers block.
        usersettings_path = None
        try:
            from coderai.config import get_user_settings_path

            usersettings_path = get_user_settings_path()
        except Exception:
            usersettings_path = None
        cleared = 0
        if isinstance(settings.get("providers"), dict):
            for key in list(settings["providers"].keys()):
                prov = settings["providers"][key]
                if isinstance(prov, dict) and "api_key" in prov:
                    prov.pop("api_key", None)
                    cleared += 1
            try:
                write_settings(settings)
            except Exception:
                pass
        # Also clear env-exported keys for this process.
        for name in list(os.environ.keys()):
            if name.startswith("CODERAI_") and "KEY" in name:
                del os.environ[name]
        # Kimi parity: drop OAuth tokens + the managed provider too.
        try:
            from coderai.auth.oauth import clear_login_config, logout_all

            removed_tokens = logout_all()
            cleared_provider = clear_login_config()
            cleared += len(removed_tokens)
        except Exception:
            cleared_provider = False
        msg = f"Logged out ({cleared} provider credential(s) cleared, {len(providers)} known)."
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
