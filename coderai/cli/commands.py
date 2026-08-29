"""Canonical slash-command catalog shared by help, completion, and dispatch."""

from __future__ import annotations

from dataclasses import dataclass


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
    SlashCommand("rename", "Rename a session summary", "Session Management"),
    SlashCommand("export", "Export session history to Markdown or JSON", "Session Management"),
    SlashCommand(
        "plan",
        "Toggle or apply Plan Mode",
        "Planning & Safety",
        subcommands=("on", "off", "apply", "reset"),
    ),
    SlashCommand("undo", "Revert to a previous checkpoint", "Planning & Safety"),
    SlashCommand("diff", "Show the current unified diff", "Planning & Safety"),
    SlashCommand("continue", "Continue agent execution", "Planning & Safety"),
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
    SlashCommand("lsp", "Inspect language-server status", "Diagnostics"),
    SlashCommand(
        "mcp",
        "Inspect MCP servers, tools, prompts, and resources",
        "Tools & Analytics",
        subcommands=("reconnect", "prompts", "resources"),
    ),
    SlashCommand("tokens", "Show token usage", "Tools & Analytics", ("cost",)),
    SlashCommand("compact", "Compress conversation context", "Tools & Analytics"),
    SlashCommand("history", "Show the session timeline", "Tools & Analytics"),
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
    SlashCommand("help", "Show command help", "Utilities", ("?",)),
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
