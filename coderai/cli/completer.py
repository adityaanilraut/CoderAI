"""Readline autocompletion and command history for CoderAI interactive REPL."""

from __future__ import annotations

import atexit
import json
import pathlib
from typing import Any

from coderai.cli.file_mention import suggest_workspace_files

AVAILABLE_SLASH_COMMANDS = [
    ("/help", "Show interactive slash command help menu or /help <command>"),
    ("/?", "Show interactive slash command help menu or /? <command>"),
    ("/doctor", "Run comprehensive system health checks and connectivity diagnostics"),
    ("/plan", "Toggle Plan Mode on/off (or on, off, apply, reset)"),
    ("/undo", "Revert files and turn to previous turn checkpoint"),
    ("/diff", "Show unified diff of changes made in session"),
    ("/model", "Open interactive model selector or switch model"),
    ("/effort", "Select or change reasoning effort (low, medium, high, max, off)"),
    ("/reasoning", "Select or change reasoning effort (alias for /effort)"),
    ("/sessions", "Interactive saved sessions menu (resume, delete, fork, search)"),
    ("/resume", "Resume saved session directly by ID"),
    ("/fork", "Fork current or specified session into a new session"),
    ("/delete", "Delete a saved session"),
    ("/rm", "Delete a saved session (alias for /delete)"),
    ("/rename", "Rename active or specified session summary title"),
    ("/new", "Start a fresh session in the current project"),
    ("/init", "Initialize or update AGENTS.md guidelines for the project"),
    ("/skills", "Explore active and workspace skills"),
    ("/skill", "Load a skill into the current session"),
    ("/jobs", "Inspect and manage background bash jobs (list, kill, logs)"),
    ("/job", "Inspect and manage background bash jobs (list, kill, logs)"),
    ("/schedule", "Manage scheduled reminders and timers (after, at, every, cancel)"),
    ("/agents", "Inspect subagent hierarchy, tree, reports, and message inbox"),
    ("/subagents", "Inspect subagent hierarchy, tree, reports, and message inbox"),
    ("/teams", "Inspect active agent team members and status"),
    ("/lsp", "Inspect LSP language servers and code diagnostics"),
    ("/mcp", "Inspect connected MCP servers, tools, prompts, resources, and reconnect"),
    ("/compact", "Compress conversation history to free context window"),
    ("/tokens", "Display session token usage and context analytics"),
    ("/cost", "Display session token usage and cost analytics"),
    ("/config", "Inspect resolved workspace & user settings"),
    ("/settings", "Inspect resolved workspace & user settings (alias for /config)"),
    (
        "/permission",
        "Show or set permission preset (read-only, workspace-write, danger-full-access)",
    ),
    (
        "/permissions",
        "Show or set permission preset (alias for /permission)",
    ),
    ("/goal", "List or update session goals (add, done, cancel, start)"),
    ("/image", "Attach image file for multimodal vision analysis"),
    ("/editor", "Open external $EDITOR (nano, vim, vi) to compose prompt"),
    ("/edit", "Open external $EDITOR (alias for /editor)"),
    ("/paste", "Enter multiline paste mode until ':::' or Ctrl-D"),
    ("/history", "View turn-by-turn conversation timeline"),
    ("/export", "Export session conversation to Markdown or JSON"),
    ("/thinking", "Toggle reasoning trace display (full, summary, lite, normal)"),
    ("/raw", "Toggle display mode for viewing reasoning traces (lite, normal, raw-scrollback)"),
    ("/clear", "Clear terminal screen and redraw status bar"),
    ("/continue", "Continue bounded multi-step agent execution"),
    ("/exit", "Exit session with summary card"),
    ("/quit", "Exit session with summary card"),
]


def _get_saved_session_ids(project_root: str) -> list[str]:
    """Retrieve saved session IDs from workspace index for autocompletion."""
    try:
        index_path = pathlib.Path(project_root) / ".coderai" / "sessions" / "index.json"
        if not index_path.is_file():
            # Check user home directory fallback
            index_path = pathlib.Path.home() / ".coderai" / "sessions" / "index.json"
        if index_path.is_file():
            data = json.loads(index_path.read_text(encoding="utf-8"))
            entries = data.get("entries", [])
            return [e["id"] for e in entries if isinstance(e, dict) and "id" in e]
    except Exception:
        pass
    return []


def _get_discovered_skill_names(project_root: str) -> list[str]:
    """Retrieve skill names discovered in workspace and global directories."""
    try:
        from coderai.core.skill import list_skills

        skills = list_skills(project_root)
        return [s.name for s in skills]
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

            # Sub-argument completion for /model
            if lead_cmd in ("/model",):
                from coderai.cli.interactive_menu import CURATED_MODELS

                all_models = [name for name, _, _ in CURATED_MODELS]
                matching_models = fuzzy_filter(arg_prefix, all_models, limit=15)
                if state < len(matching_models):
                    return matching_models[state]
                return None

            # Sub-argument completion for /mcp
            if lead_cmd in ("/mcp",):
                subcmds = ["reconnect", "prompts", "resources"]
                matching_subs = fuzzy_filter(arg_prefix, subcmds)
                if state < len(matching_subs):
                    return matching_subs[state]
                return None

            # Sub-argument completion for /thinking and /raw
            if lead_cmd in ("/thinking", "/raw"):
                modes = ["full", "summary", "lite", "normal", "on", "off"]
                matching_modes = fuzzy_filter(arg_prefix, modes)
                if state < len(matching_modes):
                    return matching_modes[state]
                return None

            # Sub-argument completion for /effort and /reasoning
            if lead_cmd in ("/effort", "/reasoning"):
                efforts = ["max", "high", "medium", "low", "off"]
                matching_efforts = fuzzy_filter(arg_prefix, efforts)
                if state < len(matching_efforts):
                    return matching_efforts[state]
                return None

            # Sub-argument completion for /permission and /permissions
            if lead_cmd in ("/permission", "/permissions"):
                presets = [
                    "read-only",
                    "workspace-write",
                    "danger-full-access",
                    "local-network-read",
                    "unrestricted-read",
                ]
                matching_presets = fuzzy_filter(arg_prefix, presets)
                if state < len(matching_presets):
                    return matching_presets[state]
                return None

            # Sub-argument completion for /goal
            if lead_cmd in ("/goal",):
                actions = ["list", "add", "done", "cancel", "start"]
                matching_actions = fuzzy_filter(arg_prefix, actions)
                if state < len(matching_actions):
                    return matching_actions[state]
                return None

            # Sub-argument completion for /plan
            if lead_cmd in ("/plan",):
                plan_subs = ["on", "off", "apply", "reset"]
                matching_plans = fuzzy_filter(arg_prefix, plan_subs)
                if state < len(matching_plans):
                    return matching_plans[state]
                return None

            # Sub-argument completion for /jobs and /job
            if lead_cmd in ("/jobs", "/job"):
                job_subs = ["list", "kill", "logs"]
                matching_jobs = fuzzy_filter(arg_prefix, job_subs)
                if state < len(matching_jobs):
                    return matching_jobs[state]
                return None

            # Sub-argument completion for /schedule
            if lead_cmd in ("/schedule",):
                sched_subs = ["list", "after", "at", "every", "cancel"]
                matching_sched = fuzzy_filter(arg_prefix, sched_subs)
                if state < len(matching_sched):
                    return matching_sched[state]
                return None

            # Sub-argument completion for /agents and /subagents
            if lead_cmd in ("/agents", "/subagents"):
                agent_subs = ["list", "tree", "report", "send"]
                matching_agents = fuzzy_filter(arg_prefix, agent_subs)
                if state < len(matching_agents):
                    return matching_agents[state]
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
