"""Readline autocompletion and command history for CoderAI interactive REPL."""

from __future__ import annotations

import atexit
import pathlib
from typing import Any

from coderai.cli.file_mention import suggest_workspace_files

AVAILABLE_SLASH_COMMANDS = [
    ("/help", "Show interactive slash command help menu"),
    ("/plan", "Toggle Plan Mode on/off"),
    ("/undo", "Revert files and turn to previous turn checkpoint"),
    ("/diff", "Show unified diff of changes made in session"),
    ("/model", "Open interactive model selector or switch model"),
    ("/sessions", "Interactive saved sessions menu (resume, delete, fork)"),
    ("/resume", "Resume saved session directly by ID"),
    ("/fork", "Fork current or specified session into a new session"),
    ("/delete", "Delete a saved session"),
    ("/new", "Start a fresh session in the current project"),
    ("/skills", "Explore active and workspace skills"),
    ("/skill", "Load a skill into the current session"),
    ("/mcp", "Inspect connected MCP servers and discovered tools"),
    ("/compact", "Compress conversation history to free context window"),
    ("/tokens", "Display session token usage and context analytics"),
    ("/config", "View and inspect workspace/user configuration"),
    ("/history", "View turn-by-turn conversation timeline"),
    ("/export", "Export session conversation to Markdown or JSON"),
    ("/thinking", "Toggle reasoning trace display (full or summary)"),
    ("/clear", "Clear terminal screen and redraw status bar"),
    ("/continue", "Continue bounded multi-step agent execution"),
    ("/exit", "Exit session with summary card"),
    ("/quit", "Exit session with summary card"),
]


class CoderAICompleter:
    """Tab-autocompleter for slash commands, models, and @file workspace paths."""

    def __init__(self, project_root: str, get_active_model: Any = None) -> None:
        self.project_root = project_root
        self.get_active_model = get_active_model

    def complete(self, text: str, state: int) -> str | None:
        """Readline completion callback function."""
        try:
            import readline

            get_buf = getattr(readline, "get_line_buffer", None)
            raw_buf = get_buf() if callable(get_buf) else ""
            line_buffer = raw_buf if raw_buf else text
        except Exception:
            line_buffer = text

        if not line_buffer and text:
            line_buffer = text

        options: list[str] = []

        # 1. @file autocomplete anywhere in line
        if "@" in line_buffer:
            at_idx = line_buffer.rfind("@")
            file_query = line_buffer[at_idx + 1 :]
            if " " not in file_query:
                matching_files = suggest_workspace_files(file_query, self.project_root, limit=20)
                options = [f"@{f}" for f in matching_files]
                if state < len(options):
                    return options[state]
                return None

        # 2. Slash command autocomplete at start of line
        stripped_line = line_buffer.lstrip()
        if stripped_line.startswith("/"):
            tokens = stripped_line.split()
            from coderai.cli.fuzzy import fuzzy_filter

            if len(tokens) <= 1 and not stripped_line.endswith(" "):
                cmd_prefix = tokens[0] if tokens else "/"
                all_cmds = [cmd for cmd, _ in AVAILABLE_SLASH_COMMANDS]
                matching_cmds = fuzzy_filter(cmd_prefix, all_cmds, limit=25)
                if state < len(matching_cmds):
                    return matching_cmds[state] + " "
                return None

            # Sub-argument completion for /model
            if tokens[0] in ("/model", "/model "):
                from coderai.cli.interactive_menu import CURATED_MODELS

                model_prefix = tokens[1] if len(tokens) > 1 else ""
                all_models = [name for name, _, _ in CURATED_MODELS]
                matching_models = fuzzy_filter(model_prefix, all_models, limit=15)
                if state < len(matching_models):
                    return matching_models[state]
                return None

            # Sub-argument completion for /mcp
            if tokens[0] == "/mcp":
                subcmds = ["reconnect", "prompts", "resources"]
                prefix = tokens[1] if len(tokens) > 1 else ""
                matching_subs = fuzzy_filter(prefix, subcmds)
                if state < len(matching_subs):
                    return matching_subs[state]
                return None

            # Sub-argument completion for /thinking
            if tokens[0] == "/thinking":
                modes = ["full", "summary", "on", "off"]
                prefix = tokens[1] if len(tokens) > 1 else ""
                matching_modes = fuzzy_filter(prefix, modes)
                if state < len(matching_modes):
                    return matching_modes[state]
                return None

        if state < len(options):
            return options[state]
        return None


def get_history_file_path() -> pathlib.Path:
    """Return path to persistent history file in user home directory."""
    hist_dir = pathlib.Path.home() / ".coderai"
    try:
        hist_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return hist_dir / "history"


def setup_readline(project_root: str, get_active_model: Any = None) -> bool:
    """Configure readline for persistent command history and tab completion."""
    try:
        import readline

        parse_and_bind = getattr(readline, "parse_and_bind", None)
        if callable(parse_and_bind):
            # Configure completion key
            if "libedit" in getattr(readline, "__doc__", ""):
                parse_and_bind("bind ^I rl_complete")
            else:
                parse_and_bind("tab: complete")

        # Set delimiter characters so @ is recognized
        set_delims = getattr(readline, "set_completer_delims", None)
        if callable(set_delims):
            set_delims(" \t\n`!@#$%^&*()=+[{]}\\|;:'\",<>?")

        completer = CoderAICompleter(project_root, get_active_model)
        set_comp = getattr(readline, "set_completer", None)
        if callable(set_comp):
            set_comp(completer.complete)

        # Load history
        hist_file = get_history_file_path()
        read_hist = getattr(readline, "read_history_file", None)
        set_hist_len = getattr(readline, "set_history_length", None)
        if hist_file.is_file() and callable(read_hist):
            try:
                read_hist(str(hist_file))
                if callable(set_hist_len):
                    set_hist_len(1000)
            except Exception:
                pass

        # Save history on exit
        write_hist = getattr(readline, "write_history_file", None)

        def _save_history() -> None:
            if callable(write_hist):
                try:
                    write_hist(str(get_history_file_path()))
                except Exception:
                    pass

        atexit.register(_save_history)
        return True
    except (ImportError, AttributeError):
        return False
