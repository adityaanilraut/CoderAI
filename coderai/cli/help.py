"""Clean, modern, and simple slash-command help system for CoderAI CLI."""

from __future__ import annotations

import difflib
from typing import Any

from coderai.cli.commands import COMMAND_ALIASES, resolve_command

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
        "syntax": "/agents or /subagents [list|tree|report <id>|send <id> <msg>]",
        "summary": "Inspect hierarchical subagent runs and communicate with child agents.",
        "description": (
            "Monitor and interact with delegated subagent tasks.\n"
            "• /agents                   — List running and completed subagents\n"
            "• /agents tree              — View hierarchical tree of subagent delegation\n"
            "• /agents report <id>       — Display final report or findings from subagent\n"
            "• /agents send <id> <msg>   — Send instruction message into subagent inbox"
        ),
        "examples": [
            "/agents",
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
    "lsp": {
        "title": "Language Server Protocol (LSP)",
        "syntax": "/lsp",
        "summary": "Inspect active LSP connections, language servers, and diagnostics.",
        "description": "View language server instances, connection health, and code diagnostics.",
        "examples": ["/lsp"],
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
            ("/lsp", "", "Inspect LSP language servers and code diagnostics"),
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
            ("/exit, /quit", "", "Exit CoderAI session with summary card"),
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
