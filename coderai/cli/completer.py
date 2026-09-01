"""Readline autocompletion and command history for CoderAI interactive REPL."""

from __future__ import annotations

import atexit
import pathlib
from typing import Any

from coderai.cli.commands import completion_entries, resolve_command
from coderai.cli.file_mention import suggest_workspace_files

AVAILABLE_SLASH_COMMANDS = completion_entries()


def _get_saved_session_ids(project_root: str) -> list[str]:
    """Retrieve saved session IDs from workspace index for autocompletion."""
    try:
        from coderai.core.session_store import JsonlSessionStore

        store = JsonlSessionStore(project_root)
        data = store.load_index()
        entries = data.get("entries", [])
        ids: list[str] = []
        for e in entries:
            if isinstance(e, dict) and e.get("id"):
                sid = str(e["id"])
                ids.append(sid)
                if len(sid) > 16 and sid[:16] not in ids:
                    ids.append(sid[:16])
        return ids
    except Exception:
        pass
    return []


def _get_discovered_skill_names(project_root: str) -> list[str]:
    """Retrieve skill names discovered in workspace and global directories."""
    try:
        from coderai.core.skill import list_skills

        skills = list_skills(project_root)
        return [
            str(skill.get("name") or "")
            for skill in skills
            if isinstance(skill, dict) and skill.get("name")
        ]
    except Exception:
        return []


class CoderAICompleter:
    """Tab-autocompleter for slash commands, models, sub-arguments, and @file workspace paths."""

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
                matching_cmds = fuzzy_filter(cmd_prefix, all_cmds, limit=30)
                if state < len(matching_cmds):
                    return matching_cmds[state] + " "
                return None

            lead_cmd = tokens[0].lower()
            arg_prefix = tokens[1] if len(tokens) > 1 else ""
            command = resolve_command(lead_cmd)
            if command and command.subcommands:
                matching_subs = fuzzy_filter(arg_prefix, list(command.subcommands))
                if state < len(matching_subs):
                    return matching_subs[state]
                return None

            # Sub-argument completion for /model
            if lead_cmd in ("/model",):
                from coderai.cli.interactive_menu import CURATED_MODELS

                all_models = [name for name, _, _ in CURATED_MODELS]
                matching_models = fuzzy_filter(arg_prefix, all_models, limit=15)
                if state < len(matching_models):
                    return matching_models[state]
                return None

            # Sub-argument completion for /skill
            if lead_cmd in ("/skill",):
                skill_names = _get_discovered_skill_names(self.project_root)
                matching_skills = fuzzy_filter(arg_prefix, skill_names, limit=15)
                if state < len(matching_skills):
                    return matching_skills[state]
                return None

            # Sub-argument completion for session IDs: /resume, /fork, /delete, /rm, /rename
            if lead_cmd in ("/resume", "/fork", "/delete", "/rm", "/rename"):
                session_ids = _get_saved_session_ids(self.project_root)
                matching_ids = fuzzy_filter(arg_prefix, session_ids, limit=15)
                if state < len(matching_ids):
                    return matching_ids[state]
                return None

            # Sub-argument completion for /help and /?
            if lead_cmd in ("/help", "/?"):
                all_topics = [cmd.lstrip("/") for cmd, _ in AVAILABLE_SLASH_COMMANDS] + [
                    "shortcuts",
                    "keyboard",
                    "editor",
                    "paste",
                ]
                matching_topics = fuzzy_filter(arg_prefix, all_topics, limit=20)
                if state < len(matching_topics):
                    return matching_topics[state]
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

        # Set delimiter characters so @ and / are recognized cleanly
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
    except Exception:
        return False
