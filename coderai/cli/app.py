"""CLI — rich UI over coderai.core (mirrors DeepCode packages/cli).

Presentation layer: argparse, interactive REPL, markdown rendering, tool execution cards,
diff previews, thinking mode summaries, dynamic status bar, interactive menus, permission
flows, autocompletion, session management, and slash commands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys
from typing import Any

from coderai._version import __version__
from coderai.cli.completer import setup_readline
from coderai.cli.diff_render import render_diff_preview
from coderai.cli.exit_summary import render_exit_summary
from coderai.cli.export_render import export_session_to_json, export_session_to_markdown
from coderai.cli.file_mention import expand_file_mentions
from coderai.cli.input_engine import read_user_turn
from coderai.cli.interactive_menu import (
    prompt_plan_implementation,
    render_config_interactive,
    render_mcp_interactive,
    render_mcp_prompts,
    render_mcp_resources_async,
    render_session_history,
    render_skills_interactive,
    render_token_breakdown,
    select_model_interactive,
    select_session_interactive,
    select_undo_interactive,
)
from coderai.cli.status_bar import render_status_bar
from coderai.cli.thinking import render_thinking_block
from coderai.cli.tool_card import render_tool_card
from coderai.cli.welcome import render_welcome_screen
from coderai.core.openai_client import create_openai_client as _core_client
from coderai.core.permissions import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    append_project_permission_allows,
)
from coderai.core.prompt import list_skills, load_skill
from coderai.core.session import SessionManager, SessionMessage

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH = True
    console: Console | None = Console()
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment,misc]
    Markdown = None  # type: ignore[assignment,misc]
    Panel = None  # type: ignore[assignment,misc]
    Table = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False
    console = None


def _clear_task_cancellation() -> None:
    """Clear any pending task cancellation counter in Python 3.11+ asyncio."""
    try:
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            cancelling_fn = getattr(task, "cancelling", None)
            if callable(cancelling_fn):
                while cancelling_fn() > 0:
                    task.uncancel()
    except Exception:
        pass


ALWAYS_ALLOWED_SCOPES = {
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "mcp",
}

_THINKING_EXPANDED: bool = False


def describe_scope(scope: str) -> str:
    """Return a human-friendly description of a permission scope."""
    scope_descriptions = {
        "read-in-cwd": "reads inside this workspace",
        "read-out-cwd": "reads outside this workspace",
        "write-in-cwd": "writes inside this workspace",
        "write-out-cwd": "writes outside this workspace",
        "delete-in-cwd": "deletes inside this workspace",
        "delete-out-cwd": "deletes outside this workspace",
        "query-git-log": "Git history queries",
        "mutate-git-log": "Git history changes",
        "network": "network access",
        "mcp": "MCP tool access",
    }
    return scope_descriptions.get(scope, scope)


def get_scope_color(scope: str) -> str:
    """Return the color coding for a permission scope."""
    if scope in ("read-in-cwd", "query-git-log"):
        return "green"
    if scope in ("read-out-cwd", "write-in-cwd", "network", "mcp"):
        return "yellow"
    return "red"


def _render_markdown(text: str) -> None:
    """Render markdown text via Rich or standard output."""
    if console is not None and _RICH and Markdown is not None:
        console.print(Markdown(text))
    else:
        print(text)


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="coderai", description="coderai — Autonomous AI pair programming in your terminal."
    )
    parser.add_argument("prompt", nargs="*", help="initial prompt (non-interactive when provided)")
    parser.add_argument(
        "--prompt",
        "-p",
        dest="prompt_flag",
        type=str,
        help="Submit a prompt on launch",
    )
    parser.add_argument("--model", "-m", help="LLM model to use")
    parser.add_argument("--message", dest="message", help="send a single message and exit")
    parser.add_argument(
        "--exec",
        "-x",
        "-e",
        dest="exec_prompt",
        nargs="?",
        const=True,
        default=None,
        help="Run one prompt non-interactively (requires --prompt / -p or positional prompt)",
    )

    parser.add_argument(
        "--resume",
        "-r",
        nargs="?",
        const=True,
        default=None,
        dest="resume",
        help="Resume a specific session by its ID. Use without an ID to show session picker.",
    )
    parser.add_argument(
        "--fork",
        "-f",
        nargs="?",
        const=True,
        default=None,
        dest="fork",
        help="Fork a specific session by its ID. Use without an ID to fork the most recent session.",
    )
    parser.add_argument(
        "--last",
        "-l",
        action="store_true",
        default=False,
        dest="last",
        help="Resume the most recent session for the current project directory.",
    )
    parser.add_argument("--plan", action="store_true", help="start session in Plan Mode")
    parser.add_argument("--yes", "-y", action="store_true", help="auto-approve all permissions")
    parser.add_argument("--verbose", "-v", action="store_true", help="print debug information")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _prompt_permissions(
    requests: list[dict[str, Any]], yes: bool, plan_mode: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    """Prompt user for confirmation when tool execution requires permission."""
    replies: list[dict[str, Any]] = []
    always_allows: list[str] = []

    for idx, req in enumerate(requests, 1):
        tool_call_id = req.get("toolCallId", "")
        name = req.get("name", "Tool")
        command = str(req.get("command", "")).strip()
        description = req.get("description", "")
        scopes: list[str] = req.get("scopes") or []

        # In Plan Mode, mutating scopes are strictly forced to prompt even with --yes
        is_forced_plan_scope = plan_mode and any(s in PLAN_MODE_FORCE_ASK_SCOPES for s in scopes)

        if yes and not is_forced_plan_scope:
            replies.append({"toolCallId": tool_call_id, "permission": "allow"})
            always_allows.extend(scopes)
            continue

        if console is not None and _RICH and Panel is not None:
            scope_items = []
            for sc in scopes:
                color = get_scope_color(sc)
                scope_items.append(f"[{color}]{sc}[/] ({describe_scope(sc)})")
            scopes_str = ", ".join(scope_items) if scope_items else "[green]none[/]"

            body = f"[bold]{name}[/]: {command}"
            if description:
                body += f"\n[dim]{description}[/]"
            if is_forced_plan_scope:
                body += "\n[bold red][WARNING] Mutating action requested while in Plan Mode.[/]"
            body += f"\n[dim]Scopes:[/] {scopes_str}"

            panel = Panel(
                body,
                title=f"[bold yellow]Permission Required ({idx}/{len(requests)})[/]",
                border_style="yellow",
                padding=(0, 1),
            )
            console.print(panel)
        else:
            print(f"\n--- Permission Required ({idx}/{len(requests)}) ---")
            print(f"[{name}] {command}")
            if description:
                print(f"  Description: {description}")
            if is_forced_plan_scope:
                print("  [WARNING] Mutating action requested while in Plan Mode.")
            if scopes:
                print(f"  Scopes: {', '.join(scopes)}")

        options: list[tuple[str, str, str]] = [("allow", "1", "Yes")]
        always_target = next((s for s in scopes if s in ALWAYS_ALLOWED_SCOPES), None)
        if always_target and not plan_mode:
            options.append(
                ("always", "2", f"Yes, and always allow {describe_scope(always_target)}")
            )
        options.append(("deny", "3" if (always_target and not plan_mode) else "2", "No"))

        print("  Options:")
        for _, key, label in options:
            print(f"    {key}. {label}")

        prompt_str = "  Allow? [1/2/3] (or y/n/a): "
        try:
            raw_choice = input(prompt_str).strip().lower()
        except (EOFError, KeyboardInterrupt):
            _clear_task_cancellation()
            raw_choice = "n"

        if raw_choice in ("2", "a", "always") and always_target and not plan_mode:
            replies.append({"toolCallId": tool_call_id, "permission": "allow"})
            always_allows.append(always_target)
        elif raw_choice in ("3", "n", "no", "deny") or (
            raw_choice == "2" and (not always_target or plan_mode)
        ):
            replies.append({"toolCallId": tool_call_id, "permission": "deny"})
        else:
            replies.append({"toolCallId": tool_call_id, "permission": "allow"})

    return replies, always_allows


def _prompt_user_questions(questions: list[dict[str, Any]]) -> str:
    """Prompt the user interactively when an AskUserQuestion tool execution occurs."""
    answers: list[str] = []

    for idx, item in enumerate(questions, 1):
        q_text = item.get("question", "")
        options = item.get("options") or []
        multi_select = bool(item.get("multiSelect", False))

        if console is not None and _RICH and Panel is not None:
            body = f"[bold white]{q_text}[/]\n"
            mode_text = (
                "[dim cyan](multi-select)[/]" if multi_select else "[dim cyan](single-select)[/]"
            )
            body += f"[dim]Mode:[/] {mode_text}\n\n"
            for o_idx, opt in enumerate(options, 1):
                label = opt.get("label", "")
                desc = f" — [dim]{opt['description']}[/]" if opt.get("description") else ""
                body += f"  [bold cyan]{o_idx}.[/] {label}{desc}\n"
            body += f"  [bold cyan]{len(options) + 1}.[/] Other (type custom response)"
            panel = Panel(
                body,
                title=f"[bold yellow]Question {idx}/{len(questions)}[/]",
                border_style="cyan",
            )
            console.print(panel)
        else:
            print(f"\n--- Question {idx}/{len(questions)} ---")
            print(q_text)
            print(f"Mode: {'multi-select' if multi_select else 'single-select'}")
            for o_idx, opt in enumerate(options, 1):
                desc = f" ({opt['description']})" if opt.get("description") else ""
                print(f"  {o_idx}. {opt.get('label', '')}{desc}")
            print(f"  {len(options) + 1}. Other (type custom response)")

        try:
            raw_ans = input("\nYour answer (choose number or type text): ").strip()
        except (EOFError, KeyboardInterrupt):
            _clear_task_cancellation()
            raw_ans = ""

        # Map number back to option label if numerical
        selected_labels: list[str] = []
        if multi_select and "," in raw_ans:
            tokens = [t.strip() for t in raw_ans.split(",")]
            for tok in tokens:
                if tok.isdigit() and 1 <= int(tok) <= len(options):
                    selected_labels.append(options[int(tok) - 1].get("label", ""))
                elif tok:
                    selected_labels.append(tok)
            final_ans = ", ".join(selected_labels) if selected_labels else raw_ans
        elif raw_ans.isdigit() and 1 <= int(raw_ans) <= len(options):
            final_ans = options[int(raw_ans) - 1].get("label", "")
        else:
            final_ans = raw_ans

        if final_ans:
            answers.append(f"{q_text}: {final_ans}")

    return "\n".join(answers) if answers else "User responded."


class _StreamState:
    """Track streaming progress to prevent jitter and duplicate renders."""

    def __init__(self) -> None:
        self.streamed_content: list[str] = []
        self.is_streaming: bool = False

    def reset(self) -> None:
        self.streamed_content.clear()
        self.is_streaming = False

    def on_chunk(self, chunk: str) -> None:
        if chunk:
            self.streamed_content.append(chunk)
            self.is_streaming = True
            sys.stdout.write(chunk)
            sys.stdout.flush()

    def had_streamed(self) -> bool:
        return bool(self.streamed_content)

    def ensure_newline(self) -> bool:
        """Ensure stream cursor is on a fresh line before printing banners or cards."""
        if self.had_streamed():
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.reset()
            return True
        return False


_STREAM_STATE = _StreamState()


def _on_assistant_message(message: SessionMessage, should_connect: bool) -> None:
    """Format and render assistant messages, thinking blocks, and tool executions."""
    # Ensure any active stream is properly closed with a newline before rendering any cards/banners
    was_streamed = _STREAM_STATE.ensure_newline()

    meta = message.meta or {}
    if meta.get("asThinking"):
        render_thinking_block(console, message.content, expanded=_THINKING_EXPANDED)
        return

    if message.role == "tool":
        render_tool_card(console, message)
        return

    if message.thinking:
        render_thinking_block(console, message.thinking, expanded=_THINKING_EXPANDED)

    if message.content and not was_streamed:
        _render_markdown(message.content)

    if message.tool_calls:
        for tc in message.tool_calls:
            name = (
                tc.get("function", {}).get("name", "")
                if isinstance(tc, dict)
                else getattr(getattr(tc, "function", None), "name", "")
            )
            if console is not None and _RICH:
                console.print(f"  [dim]→ invoking [bold cyan]{name}[/][/]")
            else:
                print(f"  → invoking {name}")


def _build_manager(project_root: str, model: str | None) -> SessionManager:
    """Build and initialize the core SessionManager."""
    from coderai.core.settings import resolve_current_settings

    resolved = resolve_current_settings(project_root)
    if model:
        resolved["model"] = model

    def get_settings() -> dict[str, Any]:
        return resolved

    mgr: SessionManager | None = None

    def create_client() -> dict[str, Any]:
        active_model = mgr.get_active_model() if mgr is not None else model
        return _core_client(project_root, model_override=active_model)

    def on_stream_chunk(chunk: str) -> None:
        _STREAM_STATE.on_chunk(chunk)

    mgr = SessionManager(
        project_root=project_root,
        create_openai_client=create_client,
        get_resolved_settings=get_settings,
        render_markdown=lambda t: t,
        on_assistant_message=_on_assistant_message,
        on_stream_chunk=on_stream_chunk,
    )
    if model:
        mgr.set_model(model)
    return mgr


async def _drain_pending_interactions(mgr: SessionManager, session_id: str, yes: bool) -> None:
    """Drain permissions and interactive user questions until session reaches a stable state."""
    while True:
        entry = mgr.get_session(session_id)
        if entry is None:
            return

        if entry.status == "ask_permission":
            replies, always = _prompt_permissions(
                entry.ask_permissions or [], yes, plan_mode=bool(entry.plan_mode)
            )
            if always:
                append_project_permission_allows(mgr.project_root, always)
            _STREAM_STATE.reset()
            await mgr.reply_session(session_id, None, permission_replies=replies)
            continue

        if entry.status in ("ask_user_question", "waiting_for_user"):
            # Extract question items from latest tool message
            messages = mgr.list_session_messages(session_id)
            latest_tool = next(
                (m for m in reversed(messages) if m.role == "tool" and not m.compacted), None
            )
            questions: list[dict[str, Any]] = []
            if latest_tool and latest_tool.content:
                try:
                    payload = json.loads(latest_tool.content)
                    if isinstance(payload.get("metadata"), dict):
                        questions = payload["metadata"].get("questions") or []
                except Exception:
                    questions = []

            if not questions and entry.ask_permissions:
                questions = entry.ask_permissions

            if questions:
                answers_text = _prompt_user_questions(questions)
                _STREAM_STATE.reset()
                await mgr.reply_session(session_id, user_prompt=answers_text)
                continue
            return

        break


def _render_help_menu() -> None:
    """Display interactive command help table grouped by category."""
    if console is not None and _RICH and Table is not None:
        table = Table(
            title="CoderAI Interactive Slash Commands",
            border_style="cyan",
            header_style="bold cyan",
        )
        table.add_column("Category", style="dim cyan", width=18)
        table.add_column("Command", style="bold white", width=22)
        table.add_column("Description", style="white")

        table.add_row("Session Management", "/new", "Start a fresh session in the workspace")
        table.add_row(
            "Session Management", "/init", "Initialize or update AGENTS.md contributor guidelines"
        )
        table.add_row(
            "Session Management", "/sessions", "Interactive session browser (resume, delete, fork)"
        )
        table.add_row("Session Management", "/resume <id>", "Resume a saved session by ID directly")
        table.add_row(
            "Session Management", "/fork [id]", "Fork current or target session into new branch"
        )
        table.add_row("Session Management", "/delete <id>", "Delete a saved session from workspace")
        table.add_row(
            "Session Management", "/export [file]", "Export session history to Markdown or JSON"
        )

        table.add_section()
        table.add_row(
            "Planning & Rollback", "/plan", "Toggle Plan Mode (strict read-only safety boundary)"
        )
        table.add_row(
            "Planning & Rollback",
            "/undo",
            "Interactive turn & checkpoint rollback (code, conversation, or both)",
        )
        table.add_row("Planning & Rollback", "/diff", "Show syntax-highlighted diff of changes")
        table.add_row(
            "Planning & Rollback", "/continue", "Continue bounded multi-step agent execution"
        )

        table.add_section()
        table.add_row(
            "Models & Skills", "/model [name]", "Interactive model selector or switch directly"
        )
        table.add_row(
            "Models & Skills", "/skills", "Explore active and discovered workspace skills"
        )
        table.add_row("Models & Skills", "/skill <name>", "Load a skill into the current session")
        table.add_row(
            "Models & Skills",
            "/thinking, /raw",
            "Toggle full reasoning trace or summary (lite/normal)",
        )

        table.add_section()
        table.add_row(
            "Tools & Analytics",
            "/mcp [subcommand]",
            "Inspect MCP servers/tools, /mcp prompts, /mcp resources, /mcp reconnect",
        )
        table.add_row(
            "Tools & Analytics", "/tokens, /cost", "View detailed token usage and context analytics"
        )
        table.add_row(
            "Tools & Analytics", "/compact", "Compress history to free up active context tokens"
        )
        table.add_row("Tools & Analytics", "/history", "View turn-by-turn conversation timeline")
        table.add_row("Tools & Analytics", "/config", "Inspect resolved workspace & user settings")
        table.add_row(
            "Tools & Analytics",
            "/permission [preset]",
            "Show or set permission preset: read-only, workspace-write, danger-full-access",
        )
        table.add_row("Tools & Analytics", "/goal [add|done] ...", "List or update session goals")

        table.add_section()
        table.add_row("Utilities", "/clear", "Clear terminal screen and redraw status")
        table.add_row("Utilities", "/help, /?", "Show this command help menu")
        table.add_row("Utilities", "/exit, /quit", "Exit CoderAI session with summary card")

        console.print()
        console.print(table)
        console.print(
            "[dim]Tip: You can mention workspace files anywhere with [bold cyan]@file.py[/] or [bold cyan]@file.py:10-30[/][/]\n"
        )
    else:
        print("\n--- CoderAI Slash Commands ---")
        print("Session Management:")
        print("  /new               Start a fresh session")
        print("  /init              Initialize or update AGENTS.md guidelines")
        print("  /sessions          Interactive sessions menu (resume, delete, fork)")
        print("  /resume <id>       Resume session by ID directly")
        print("  /fork [id]         Fork session into a new branch")
        print("  /delete <id>       Delete a saved session")
        print("  /export [file]     Export session to Markdown/JSON")
        print("\nPlanning & Rollback:")
        print("  /plan              Toggle Plan Mode on/off")
        print("  /undo              Revert to previous checkpoint")
        print("  /diff              Show unified diff of changes")
        print("  /continue          Continue agent execution")
        print("\nModels & Skills:")
        print("  /model [name]      Select or switch active model")
        print("  /skills            List available workspace skills")
        print("  /skill <name>      Load skill into current session")
        print("  /thinking, /raw    Toggle reasoning trace (full/summary)")
        print("\nTools & Analytics:")
        print("  /mcp               Inspect MCP servers and tools")
        print("  /tokens            Show token usage breakdown")
        print("  /compact           Compress conversation context")
        print("  /history           View session timeline")
        print("  /config            View active configuration")
        print("\nUtilities:")
        print("  /clear             Clear terminal screen")
        print("  /help              Show this help menu")
        print("  /exit, /quit       Quit CoderAI\n")


def _show_diff(mgr: SessionManager, session_id: str | None) -> None:
    """Display the unified diff of changes made in the session or git workspace."""
    diff_output = ""
    if session_id:
        diff_output = mgr.get_diff(session_id)

    if not diff_output.strip():
        # Fallback to workspace git diff if in a git repository
        try:
            res = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=mgr.project_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                diff_output = res.stdout
        except Exception:
            pass

    if not diff_output.strip():
        if console is not None and _RICH:
            console.print("[dim]No file changes detected since session start.[/]")
        else:
            print("No file changes detected since session start.")
        return

    render_diff_preview(console, diff_output, title="Session File Diffs")


def _queue_skill(
    mgr: SessionManager,
    ui_console: Any,
    name: str,
    pending_skills: list[str],
    *,
    quiet_unknown: bool = False,
) -> bool:
    if not name.strip():
        print("Usage: /skill <name>")
        return False
    skill = load_skill(name, mgr.project_root)
    if not skill:
        if not quiet_unknown:
            print(f"Unknown skill: {name}")
        return False
    if skill["name"] not in pending_skills:
        pending_skills.append(skill["name"])
    loaded_msg = f"Skill '{skill['name']}' ready."
    if ui_console is not None and _RICH:
        ui_console.print(f"[bold green]{loaded_msg}[/]")
    else:
        print(loaded_msg)
    return True


async def _run_interactive(
    mgr: SessionManager,
    yes: bool,
    resume: str | bool | None = None,
    fork: str | bool | None = None,
    last: bool = False,
    plan_mode: bool = False,
    initial_prompt: str | None = None,
) -> int:
    """Interactive REPL with rich welcome screen, dynamic status bar, and command selectors."""
    global _THINKING_EXPANDED

    session_id: str | None = None
    active_plan_mode = plan_mode
    pending_skills: list[str] = []

    # 1. Handle --last
    if last:
        sessions = mgr.list_sessions()
        if sessions:
            session_id = sessions[0].id
        else:
            if console is not None and _RICH:
                console.print(
                    "[yellow]No previous sessions found for this project. Starting a new session.[/]"
                )
            else:
                print("No previous sessions found for this project. Starting a new session.")

    # 2. Handle --fork
    elif fork is not None:
        if isinstance(fork, str) and fork.strip():
            target_id = fork.strip()
        else:
            sessions = mgr.list_sessions()
            if not sessions:
                print("No previous session found to fork.")
                return 1
            target_id = sessions[0].id
        forked_id = mgr.fork_session(target_id)
        if not forked_id:
            print(f"Failed to fork session '{target_id}'.")
            return 1
        session_id = forked_id

    # 3. Handle --resume
    elif resume is not None:
        if resume is True:
            sessions = mgr.list_sessions()[:15]
            if not sessions:
                print("No saved sessions found.")
            else:
                chosen_action = select_session_interactive(console, sessions)
                if chosen_action:
                    if chosen_action.startswith("fork:"):
                        fork_target = chosen_action.split(":", 1)[1]
                        session_id = mgr.fork_session(fork_target)
                    elif chosen_action.startswith("delete:"):
                        del_target = chosen_action.split(":", 1)[1]
                        mgr.delete_session(del_target)
                        session_id = None
                    else:
                        session_id = chosen_action
        elif isinstance(resume, str) and resume.strip():
            session_id = resume.strip()
            if mgr.get_session(session_id) is None:
                print(f"No saved session with id '{session_id}'.")
                return 1

    if session_id:
        resumed_entry = mgr.get_session(session_id)
        if resumed_entry:
            active_plan_mode = resumed_entry.plan_mode

    # Setup Readline Persistent History and Autocompletion
    setup_readline(mgr.project_root, mgr.get_active_model)

    # Setup Custom SIGINT Handler for Interactive REPL
    active_turn_task: asyncio.Task[Any] | None = None

    def _sigint_handler(signum: int, frame: Any) -> None:
        nonlocal active_turn_task
        if active_turn_task is not None and not active_turn_task.done():
            active_turn_task.cancel()
        else:
            raise KeyboardInterrupt()

    old_sigint_handler = None
    try:
        old_sigint_handler = signal.signal(signal.SIGINT, _sigint_handler)
    except (ValueError, AttributeError):
        pass

    # Render Welcome Screen & Brand Identity
    mcp_count = len(getattr(mgr.mcp_manager, "clients", {}) or {})
    discovered_skills = list_skills(mgr.project_root)
    render_welcome_screen(
        console,
        mgr.project_root,
        mgr.get_active_model(),
        plan_mode=active_plan_mode,
        mcp_servers_count=mcp_count,
        skills_count=len(discovered_skills),
    )

    # If an initial prompt was provided alongside interactive launch
    if initial_prompt and initial_prompt.strip():
        effective_prompt, attached_files = expand_file_mentions(
            initial_prompt.strip(), mgr.project_root
        )
        if attached_files:
            if console is not None and _RICH:
                console.print(
                    f"  [dim]📎 Attached files:[/] [bold cyan]{', '.join(attached_files)}[/]"
                )
            else:
                print(f"  Attached files: {', '.join(attached_files)}")
        _STREAM_STATE.reset()

        async def _run_initial() -> str | None:
            nonlocal session_id
            if session_id is None:
                s_id = await mgr.create_session(effective_prompt, plan_mode=active_plan_mode)
            else:
                s_id = session_id
                await mgr.reply_session(session_id, effective_prompt, plan_mode=active_plan_mode)
            await _drain_pending_interactions(mgr, s_id, yes)
            return s_id

        active_turn_task = asyncio.create_task(_run_initial())
        try:
            res_id = await active_turn_task
            if session_id is None and res_id:
                session_id = res_id
        except (KeyboardInterrupt, asyncio.CancelledError):
            _clear_task_cancellation()
            if session_id:
                mgr.interrupt_session(session_id)
            if console is not None and _RICH:
                console.print("\n[bold yellow]Turn interrupted by user.[/]")
            else:
                print("\nTurn interrupted by user.")
        finally:
            active_turn_task = None

    try:
        while True:
            # Render Dynamic Status Bar
            cur_entry = mgr.get_session(session_id) if session_id else None
            tokens_count = cur_entry.active_tokens if cur_entry else 0
            messages_list = mgr.list_session_messages(session_id) if session_id else []
            turns_count = sum(1 for m in messages_list if m.role == "user")
            active_mcp_count = len(getattr(mgr.mcp_manager, "clients", {}) or {})

            render_status_bar(
                console,
                mgr.get_active_model(),
                tokens_count,
                active_plan_mode,
                mgr.project_root,
                turns=turns_count,
                mcp_count=active_mcp_count,
                settings=mgr.get_resolved_settings(),
            )

            try:
                prompt_label = "coderai [plan] ❯ " if active_plan_mode else "coderai ❯ "
                raw = read_user_turn(prompt_label).strip()
            except KeyboardInterrupt:
                _clear_task_cancellation()
                print()
                continue
            except EOFError:
                break

            if not raw:
                continue

            if raw.startswith("/"):
                tokens = raw.split()
                cmd = tokens[0].lower()
                cmd_arg = " ".join(tokens[1:]).strip() if len(tokens) > 1 else ""

                if cmd in ("/exit", "/quit"):
                    break

                if cmd in ("/help", "/?"):
                    _render_help_menu()
                    continue

                if cmd == "/clear":
                    os.system("cls" if os.name == "nt" else "clear")
                    continue

                if cmd == "/tokens" or cmd == "/cost":
                    render_token_breakdown(console, mgr, session_id)
                    continue

                if cmd in ("/config", "/settings"):
                    render_config_interactive(console, mgr.project_root)
                    continue

                if cmd in ("/permission", "/permissions"):
                    from coderai.core.sandbox import (
                        SANDBOX_MODES,
                        parse_sandbox_mode,
                        preset_permissions,
                    )
                    from coderai.core.settings import read_project_settings, write_project_settings

                    arg = cmd_arg.strip().lower()
                    if not arg:
                        perms = mgr.get_resolved_settings().get("permissions") or {}
                        msg = (
                            f"Permission preset: {perms.get('preset') or 'unset (danger-full-access default)'}\n"
                            f"Sandbox: {perms.get('sandbox')}\n"
                            f"allow={perms.get('allow')}\n"
                            f"deny={perms.get('deny')}\n"
                            f"ask={perms.get('ask')}\n"
                            f"Usage: /permission {' | '.join(SANDBOX_MODES)}"
                        )
                        print(msg)
                        continue
                    parsed = parse_sandbox_mode(arg)
                    if not parsed:
                        print(f"Unknown preset '{cmd_arg}'. Use: {', '.join(SANDBOX_MODES)}")
                        continue
                    settings = read_project_settings(mgr.project_root) or {}
                    permissions = dict(settings.get("permissions") or {})
                    mapped = preset_permissions(parsed)
                    permissions.update(
                        {
                            "preset": parsed,
                            "allow": mapped["allow"],
                            "deny": mapped["deny"],
                            "ask": mapped["ask"],
                            "defaultMode": mapped["defaultMode"],
                        }
                    )
                    settings["permissions"] = permissions
                    settings["permissionPreset"] = parsed
                    write_project_settings(settings, mgr.project_root)
                    print(f"Permission preset set to {parsed}. New sessions will use this preset.")
                    continue

                if cmd == "/goal":
                    from coderai.core.goals import get_goal_store

                    store = get_goal_store(mgr.project_root)
                    sid = session_id or "default"
                    tokens = cmd_arg.split(None, 1)
                    action = tokens[0].lower() if tokens else "list"
                    rest = tokens[1].strip() if len(tokens) > 1 else ""
                    if action in ("", "list"):
                        print(store.format(sid))
                    elif action == "add" and rest:
                        goal = store.add(sid, rest)
                        print(f"Added goal {goal.id}: {goal.title}")
                    elif action in ("done", "cancel", "start") and rest:
                        updated = store.update(
                            sid,
                            rest,
                            status={"done": "done", "cancel": "cancelled", "start": "in_progress"}[
                                action
                            ],
                        )
                        print(f"Updated {updated.id}" if updated else f"Unknown goal '{rest}'")
                    else:
                        print("Usage: /goal [list|add <title>|done <id>|cancel <id>]")
                    continue

                if cmd == "/history":
                    render_session_history(console, mgr, session_id)
                    continue

                if cmd == "/mcp":
                    if cmd_arg.startswith("reconnect"):
                        server_name = cmd_arg.replace("reconnect", "", 1).strip()
                        if not server_name:
                            print("Usage: /mcp reconnect <server_name>")
                            continue
                        reconnected = await mgr.mcp_manager.reconnect(server_name)
                        mgr._refresh_mcp_tool_definitions()
                        if reconnected:
                            if console is not None and _RICH:
                                console.print(
                                    f"[bold green]✓ Reconnected MCP server '[cyan]{server_name}[/]'.[/]"
                                )
                            else:
                                print(f"✓ Reconnected MCP server '{server_name}'.")
                        else:
                            status = next(
                                (
                                    s
                                    for s in mgr.mcp_manager.server_statuses
                                    if s.name == server_name
                                ),
                                None,
                            )
                            err_msg = f": {status.error}" if status and status.error else ""
                            if console is not None and _RICH:
                                console.print(
                                    f"[bold red]Failed to reconnect MCP server '{server_name}'{err_msg}[/]"
                                )
                            else:
                                print(f"Failed to reconnect MCP server '{server_name}'{err_msg}")
                        continue
                    elif cmd_arg.startswith("prompts"):
                        render_mcp_prompts(console, mgr)
                        continue
                    elif cmd_arg.startswith("resources"):
                        uri_arg = cmd_arg.replace("resources", "", 1).strip() or None
                        await render_mcp_resources_async(console, mgr, uri=uri_arg)
                        continue
                    else:
                        render_mcp_interactive(console, mgr)
                        continue

                if cmd in ("/thinking", "/raw"):
                    arg = cmd_arg.lower()
                    if arg in ("full", "on", "expand", "expanded", "normal", "raw-scrollback"):
                        _THINKING_EXPANDED = True
                        if console is not None and _RICH:
                            console.print(
                                "[bold magenta]Reasoning traces:[/] Full expanded view enabled."
                            )
                        else:
                            print("Reasoning traces: Full expanded view enabled.")
                    elif arg in ("summary", "off", "collapse", "collapsed", "lite"):
                        _THINKING_EXPANDED = False
                        if console is not None and _RICH:
                            console.print(
                                "[bold magenta]Reasoning traces:[/] Concise summary view enabled."
                            )
                        else:
                            print("Reasoning traces: Concise summary view enabled.")
                    else:
                        _THINKING_EXPANDED = not _THINKING_EXPANDED
                        mode_str = "Full expanded" if _THINKING_EXPANDED else "Concise summary"
                        if console is not None and _RICH:
                            console.print(
                                f"[bold magenta]Reasoning traces:[/] Switched to {mode_str}."
                            )
                        else:
                            print(f"Reasoning traces: Switched to {mode_str}.")
                    continue

                if cmd == "/export":
                    if not session_id:
                        print("No active session to export.")
                        continue
                    if cmd_arg.endswith(".json"):
                        exported_file = export_session_to_json(mgr, session_id, cmd_arg)
                    else:
                        exported_file = export_session_to_markdown(
                            mgr, session_id, cmd_arg if cmd_arg else None
                        )
                    if console is not None and _RICH:
                        console.print(
                            f"[bold green]✓ Session successfully exported to:[/] [cyan]{exported_file}[/]"
                        )
                    else:
                        print(f"✓ Session successfully exported to: {exported_file}")
                    continue

                if cmd == "/fork":
                    target_to_fork = cmd_arg.strip() if cmd_arg else session_id
                    if not target_to_fork:
                        print("No active session to fork. Usage: /fork <session_id>")
                        continue
                    forked_id = mgr.fork_session(target_to_fork)
                    if forked_id:
                        session_id = forked_id
                        resumed_entry = mgr.get_session(session_id)
                        if resumed_entry:
                            active_plan_mode = resumed_entry.plan_mode
                        if console is not None and _RICH:
                            console.print(
                                f"[bold green]✓ Forked and switched to session:[/] {session_id}"
                            )
                        else:
                            print(f"Forked and switched to session: {session_id}")
                    else:
                        print(f"Failed to fork session '{target_to_fork}'.")
                    continue

                if cmd in ("/delete", "/rm"):
                    del_target_id = cmd_arg if cmd_arg else session_id
                    if not del_target_id:
                        print("Usage: /delete <session_id>")
                        continue
                    if mgr.delete_session(del_target_id):
                        if session_id == del_target_id:
                            session_id = None
                        if console is not None and _RICH:
                            console.print(
                                f"[bold green]✓ Deleted session:[/] [red]{del_target_id}[/]"
                            )
                        else:
                            print(f"✓ Deleted session: {del_target_id}")
                    else:
                        print(f"No saved session with id '{del_target_id}'.")
                    continue

                if cmd == "/compact":
                    if not session_id:
                        print("No active session to compact.")
                        continue
                    _STREAM_STATE.reset()
                    if console is not None and _RICH:
                        with console.status("[bold cyan]Compacting session context...[/]"):
                            await mgr.compact_session(session_id)
                    else:
                        await mgr.compact_session(session_id)
                    entry = mgr.get_session(session_id)
                    active_tokens = entry.active_tokens if entry else 0
                    if console is not None and _RICH:
                        console.print(
                            f"[bold green]✓[/] Session context compacted. Active tokens: [bold cyan]{active_tokens}[/]"
                        )
                    else:
                        print(f"✓ Session context compacted. Active tokens: {active_tokens}")
                    continue

                if cmd == "/continue":
                    if not session_id:
                        print("No active session to continue.")
                        continue
                    _STREAM_STATE.reset()
                    await mgr.reply_session(session_id, "/continue")
                    await _drain_pending_interactions(mgr, session_id, yes)
                    continue

                if cmd == "/plan":
                    sub = cmd_arg.lower()
                    if sub == "on":
                        active_plan_mode = True
                    elif sub == "off":
                        active_plan_mode = False
                    elif sub == "reset":
                        active_plan_mode = False
                        if session_id:
                            entry = mgr.get_session(session_id)
                            if entry:
                                entry.plan_mode = False
                        if console is not None and _RICH:
                            console.print("[bold cyan]Plan mode state reset to default.[/]")
                        else:
                            print("Plan mode state reset to default.")
                        continue
                    elif sub == "apply":
                        if not session_id:
                            print("No active session to apply plan for.")
                            continue
                        active_plan_mode = False
                        if console is not None and _RICH:
                            console.print(
                                "[bold green]Applying approved plan! Exiting Plan Mode and beginning implementation...[/]"
                            )
                        else:
                            print(
                                "Applying approved plan! Exiting Plan Mode and beginning implementation..."
                            )
                        _STREAM_STATE.reset()
                        current_session_id = session_id

                        async def _run_apply() -> None:
                            await mgr.reply_session(
                                current_session_id,
                                "Proceed with the implementation of the approved plan.",
                                plan_mode=False,
                            )
                            await _drain_pending_interactions(mgr, current_session_id, yes)

                        active_turn_task = asyncio.create_task(_run_apply())
                        try:
                            await active_turn_task
                        except (KeyboardInterrupt, asyncio.CancelledError):
                            _clear_task_cancellation()
                            if session_id:
                                mgr.interrupt_session(session_id)
                            if console is not None and _RICH:
                                console.print("\n[bold yellow]Turn interrupted by user.[/]")
                            else:
                                print("\nTurn interrupted by user.")
                        finally:
                            active_turn_task = None
                        continue
                    elif not sub:
                        active_plan_mode = not active_plan_mode
                    else:
                        print("Usage: /plan [on|off|apply|reset]")
                        continue

                    mode_str = (
                        "ON (read-only architectural planning)" if active_plan_mode else "OFF"
                    )
                    if console is not None and _RICH:
                        console.print(f"[bold cyan]Plan mode:[/] {mode_str}")
                    else:
                        print(f"Plan mode: {mode_str}")
                    continue

                if cmd == "/diff":
                    _show_diff(mgr, session_id)
                    continue

                if cmd == "/model":
                    if not cmd_arg:
                        chosen_model = select_model_interactive(console, mgr.get_active_model())
                        if chosen_model != mgr.get_active_model():
                            mgr.set_model(chosen_model)
                            if console is not None and _RICH:
                                console.print(
                                    f"[bold green]Switched active model to:[/] {chosen_model}"
                                )
                            else:
                                print(f"Switched active model to: {chosen_model}")
                    else:
                        target_model = cmd_arg.strip()
                        if target_model.isdigit():
                            from coderai.cli.interactive_menu import CURATED_MODELS

                            idx = int(target_model)
                            if 1 <= idx <= len(CURATED_MODELS):
                                target_model = CURATED_MODELS[idx - 1][0]
                        mgr.set_model(target_model)
                        if console is not None and _RICH:
                            console.print(
                                f"[bold green]Switched active model to:[/] {target_model}"
                            )
                        else:
                            print(f"Switched active model to: {target_model}")
                    continue

                if cmd == "/sessions":
                    sessions = mgr.list_sessions()[:15]
                    chosen_action = select_session_interactive(console, sessions)
                    if chosen_action:
                        if chosen_action.startswith("delete:"):
                            del_target = chosen_action.split(":", 1)[1]
                            if mgr.delete_session(del_target):
                                if session_id == del_target:
                                    session_id = None
                                if console is not None and _RICH:
                                    console.print(f"[bold green]✓ Deleted session:[/] {del_target}")
                                else:
                                    print(f"Deleted session: {del_target}")
                        elif chosen_action.startswith("fork:"):
                            fork_target = chosen_action.split(":", 1)[1]
                            forked_id = mgr.fork_session(fork_target)
                            if forked_id:
                                session_id = forked_id
                                resumed_entry = mgr.get_session(session_id)
                                if resumed_entry:
                                    active_plan_mode = resumed_entry.plan_mode
                                if console is not None and _RICH:
                                    console.print(
                                        f"[bold green]✓ Forked and switched to session:[/] {session_id}"
                                    )
                                else:
                                    print(f"Forked and switched to session: {session_id}")
                        else:
                            session_id = chosen_action
                            resumed_entry = mgr.get_session(session_id)
                            if resumed_entry:
                                active_plan_mode = resumed_entry.plan_mode
                            if console is not None and _RICH:
                                console.print(f"[bold green]Resumed session:[/] {session_id}")
                            else:
                                print(f"Resumed session: {session_id}")
                    continue

                if cmd == "/skills":
                    render_skills_interactive(console, mgr.project_root)
                    continue

                if cmd == "/skill":
                    if _queue_skill(mgr, console, cmd_arg, pending_skills):
                        if session_id:
                            mgr.inject_skills(session_id, pending_skills)
                            pending_skills.clear()
                    continue

                if cmd == "/undo":
                    if not session_id:
                        print("No active session to undo.")
                        continue
                    targets = mgr.list_undo_targets(session_id)
                    if not targets:
                        print("Nothing to undo in the active session.")
                        continue
                    target, mode = select_undo_interactive(console, targets)
                    if target:
                        success = mgr.undo(
                            session_id, target_message_id=target["message_id"], mode=mode
                        )
                        if success:
                            mode_desc = {
                                "restore_both": "reverted files and history",
                                "restore_conversation_only": "rolled back conversation history",
                                "restore_code_only": "reverted files on disk",
                            }.get(mode, "reverted")
                            if console is not None and _RICH:
                                console.print(
                                    f"[bold green]✓ Successfully {mode_desc} to Turn #{target['turn_index']}.[/]"
                                )
                            else:
                                print(
                                    f"✓ Successfully {mode_desc} to Turn #{target['turn_index']}."
                                )
                        else:
                            print(f"Failed to undo to Turn #{target['turn_index']}.")
                    continue

                if cmd == "/new":
                    session_id = None
                    if console is not None and _RICH:
                        console.print("[bold cyan]Started a fresh session.[/]")
                    else:
                        print("Started a fresh session.")
                    continue

                if cmd == "/init":
                    _STREAM_STATE.reset()

                    async def _run_init() -> str | None:
                        nonlocal session_id
                        if session_id is None:
                            s_id = await mgr.create_session("/init", plan_mode=active_plan_mode)
                        else:
                            s_id = session_id
                            await mgr.reply_session(session_id, "/init", plan_mode=active_plan_mode)
                        await _drain_pending_interactions(mgr, s_id, yes)
                        return s_id

                    active_turn_task = asyncio.create_task(_run_init())
                    try:
                        res_id = await active_turn_task
                        if session_id is None and res_id:
                            session_id = res_id
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        _clear_task_cancellation()
                        if session_id:
                            mgr.interrupt_session(session_id)
                        if console is not None and _RICH:
                            console.print("\n[bold yellow]Turn interrupted by user.[/]")
                        else:
                            print("\nTurn interrupted by user.")
                    finally:
                        active_turn_task = None
                    continue

                if cmd == "/resume":
                    if not cmd_arg:
                        print("Usage: /resume <session_id>")
                        continue
                    session_id = cmd_arg
                    resumed = mgr.get_session(session_id)
                    if resumed is None:
                        print(f"No saved session with id '{session_id}'.")
                        session_id = None
                    else:
                        active_plan_mode = resumed.plan_mode
                        print(f"Resumed session {session_id}.")
                    continue

                skill_alias = cmd.lstrip("/")
                if _queue_skill(mgr, console, skill_alias, pending_skills, quiet_unknown=True):
                    if session_id:
                        mgr.inject_skills(session_id, pending_skills)
                        pending_skills.clear()
                    continue

                print(f"Unknown command: {raw}. Type /help for available commands.")
                continue

            # Process @file mentions in user input
            effective_prompt, attached_files = expand_file_mentions(raw, mgr.project_root)
            if attached_files:
                if console is not None and _RICH:
                    console.print(
                        f"  [dim]📎 Attached files:[/] [bold cyan]{', '.join(attached_files)}[/]"
                    )
                else:
                    print(f"  Attached files: {', '.join(attached_files)}")

            _STREAM_STATE.reset()
            try:

                async def _run_user_turn() -> str | None:
                    nonlocal session_id
                    if session_id is None:
                        s_id = await mgr.create_session(
                            effective_prompt,
                            plan_mode=active_plan_mode,
                            skills=pending_skills or None,
                        )
                    else:
                        s_id = session_id
                        await mgr.reply_session(
                            session_id,
                            effective_prompt,
                            plan_mode=active_plan_mode,
                            skills=pending_skills or None,
                        )
                    pending_skills.clear()
                    await _drain_pending_interactions(mgr, s_id, yes)
                    return s_id

                active_turn_task = asyncio.create_task(_run_user_turn())
                try:
                    res_id = await active_turn_task
                    if session_id is None and res_id:
                        session_id = res_id
                finally:
                    active_turn_task = None

                # Post-plan decision prompt when a plan is proposed during plan mode
                if active_plan_mode and session_id:
                    entry = mgr.get_session(session_id)
                    reply = (entry.assistant_reply or "") if entry else ""
                    if not reply:
                        msgs = mgr.list_session_messages(session_id)
                        last_asst = next(
                            (
                                m
                                for m in reversed(msgs)
                                if m.role == "assistant" and not m.compacted
                            ),
                            None,
                        )
                        reply = last_asst.content if last_asst else ""

                    if "<proposed_plan>" in reply:
                        action = prompt_plan_implementation(console)
                        if action == "execute":
                            active_plan_mode = False
                            if console is not None and _RICH:
                                console.print(
                                    "[bold green]✓ Plan approved! Exiting Plan Mode and beginning implementation...[/]"
                                )
                            else:
                                print(
                                    "✓ Plan approved! Exiting Plan Mode and beginning implementation..."
                                )
                            _STREAM_STATE.reset()

                            async def _run_plan_execution() -> None:
                                await mgr.reply_session(
                                    session_id,
                                    "Proceed with the implementation of the approved plan.",
                                    plan_mode=False,
                                )
                                await _drain_pending_interactions(mgr, session_id, yes)

                            active_turn_task = asyncio.create_task(_run_plan_execution())
                            try:
                                await active_turn_task
                            finally:
                                active_turn_task = None
                        elif action == "refine":
                            try:
                                refine_input = input("Enter plan refinements: ").strip()
                            except (EOFError, KeyboardInterrupt):
                                _clear_task_cancellation()
                                refine_input = ""
                            if refine_input:
                                _STREAM_STATE.reset()

                                async def _run_plan_refine() -> None:
                                    await mgr.reply_session(
                                        session_id,
                                        refine_input,
                                        plan_mode=True,
                                    )
                                    await _drain_pending_interactions(mgr, session_id, yes)

                                active_turn_task = asyncio.create_task(_run_plan_refine())
                                try:
                                    await active_turn_task
                                finally:
                                    active_turn_task = None
            except (KeyboardInterrupt, asyncio.CancelledError):
                _clear_task_cancellation()
                if session_id:
                    mgr.interrupt_session(session_id)
                if console is not None and _RICH:
                    console.print("\n[bold yellow]Turn interrupted by user.[/]")
                else:
                    print("\nTurn interrupted by user.")
                continue
    finally:
        if old_sigint_handler is not None:
            try:
                signal.signal(signal.SIGINT, old_sigint_handler)
            except (ValueError, AttributeError):
                pass
        render_exit_summary(console, mgr, session_id)

    return 0


async def _run_once(mgr: SessionManager, prompt: str, yes: bool, plan_mode: bool = False) -> int:
    """Execute a single prompt non-interactively and exit."""
    effective_prompt, _ = expand_file_mentions(prompt, mgr.project_root)
    _STREAM_STATE.reset()
    try:
        session_id = await mgr.create_session(effective_prompt, plan_mode=plan_mode)
        await _drain_pending_interactions(mgr, session_id, yes)
        entry = mgr.get_session(session_id)
        if entry and entry.status == "failed":
            return 1
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        _clear_task_cancellation()
        return 0
    except Exception as e:
        if console is not None and _RICH:
            console.print(f"[bold red]Error:[/] {e}")
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Console entry point for CoderAI CLI."""
    args = _build_parser().parse_args(argv)
    project_root = str(pathlib.Path.cwd().resolve())

    # Check mutual exclusions & argument validity
    has_positional = bool(args.prompt)
    has_prompt_flag = bool(args.prompt_flag and args.prompt_flag.strip())
    has_exec = args.exec_prompt is not None and args.exec_prompt is not False
    exec_str = (
        args.exec_prompt if isinstance(args.exec_prompt, str) and args.exec_prompt.strip() else None
    )
    prompt_value = (
        args.prompt_flag
        if has_prompt_flag
        else (
            exec_str
            if exec_str
            else (" ".join(args.prompt) if has_positional else (args.message or None))
        )
    )

    if has_positional and has_prompt_flag:
        print(
            "Cannot use both a positional prompt and the --prompt (-p) flag together",
            file=sys.stderr,
        )
        return 1

    if args.last and args.resume is not None:
        print(
            "Cannot use --last together with --resume. Use --last to resume the most recent session, or --resume <sessionId> for a specific session.",
            file=sys.stderr,
        )
        return 1

    if args.fork is not None and args.resume is not None:
        print("Cannot use --fork together with --resume.", file=sys.stderr)
        return 1

    if args.last and args.fork is not None:
        print("Cannot use --last together with --fork.", file=sys.stderr)
        return 1

    if args.resume is True and prompt_value:
        print(
            "Cannot use --resume without a session ID together with --prompt.\nUse --resume <sessionId> -p <prompt> to resume a session and send a prompt.",
            file=sys.stderr,
        )
        return 1

    if has_exec and not prompt_value:
        print("--exec / -x requires a non-empty --prompt / -p value.", file=sys.stderr)
        return 1

    if has_exec and args.resume is True:
        print(
            "--exec cannot use --resume without a session ID.\nUse --exec --resume <sessionId> --prompt <prompt>.",
            file=sys.stderr,
        )
        return 1

    mgr = _build_manager(project_root, args.model)

    async def _main() -> int:
        await mgr.init_mcp_servers()
        try:
            if has_exec and prompt_value:
                from coderai.cli.exec_runner import run_exec_session

                resume_id = args.resume if isinstance(args.resume, str) else None
                return await run_exec_session(
                    prompt_value,
                    project_root=project_root,
                    model=args.model,
                    resume_session_id=resume_id,
                    plan_mode=args.plan,
                    auto_approve=args.yes,
                    verbose=args.verbose,
                )

            if prompt_value and not (args.resume or args.fork or args.last):
                return await _run_once(mgr, prompt_value, args.yes, plan_mode=args.plan)
            return await _run_interactive(
                mgr,
                args.yes,
                resume=args.resume,
                fork=args.fork,
                last=args.last,
                plan_mode=args.plan,
                initial_prompt=prompt_value,
            )
        finally:
            try:
                mgr.dispose()
            except Exception:
                pass

    try:
        return asyncio.run(_main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
