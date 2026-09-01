"""Rich terminal interface over :mod:`coderai.core`.

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
from coderai.cli.commands import parse_slash_command
from coderai.cli.completer import setup_readline
from coderai.cli.diff_render import render_diff_preview
from coderai.cli.exit_summary import render_exit_summary
from coderai.cli.export_render import export_session_to_json, export_session_to_markdown
from coderai.cli.file_mention import expand_file_mentions
from coderai.cli.help import render_help
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
    select_with_arrows,
)
from coderai.cli.session_factory import build_session_manager, close_session_manager
from coderai.cli.status_bar import render_status_bar
from coderai.cli.thinking import LiveThinkingStreamer, render_thinking_block
from coderai.cli.tool_card import render_tool_card
from coderai.cli.welcome import render_welcome_screen
from coderai.core.permissions import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    append_project_permission_allows,
)
from coderai.core.prompt_sections import TOOL_PRESETS
from coderai.core.session import SessionManager, SessionMessage
from coderai.core.skill import list_skills, load_skill

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

_RICH = True
console = Console()


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
    """Render markdown text via Rich."""
    console.print(Markdown(text))


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
        "--session",
        "-s",
        nargs="?",
        const=True,
        default=None,
        dest="resume",
        help="Resume a specific session by its ID, prefix, or checkpoint. Use without an ID to show session picker.",
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
    parser.add_argument(
        "--preset",
        dest="preset",
        choices=list(TOOL_PRESETS),
        default=None,
        help="Tool preset: full, core, or shell_edit",
    )
    parser.add_argument(
        "--setup",
        dest="setup",
        action="store_true",
        default=False,
        help="Launch interactive setup wizard to configure API keys, endpoints, and models",
    )
    parser.add_argument(
        "--provider",
        dest="setup_provider",
        help="Provider to configure in setup (e.g. openai, deepseek, gemini, anthropic, openrouter, ollama)",
    )
    parser.add_argument(
        "--key",
        dest="setup_key",
        help="API key to save for the specified provider",
    )
    parser.add_argument(
        "--base-url",
        dest="setup_base_url",
        help="Base URL endpoint to configure (for local or custom providers)",
    )
    parser.add_argument(
        "--setup-model",
        dest="setup_model",
        help="Default model to configure in setup",
    )
    parser.add_argument(
        "--test",
        dest="setup_test",
        action="store_true",
        help="Test connection and authentication with active or specified provider",
    )
    parser.add_argument(
        "--status",
        dest="setup_status",
        action="store_true",
        help="Display provider credentials and configuration status table",
    )
    parser.add_argument(
        "--project",
        dest="setup_project",
        action="store_true",
        help="Save configuration to project workspace instead of user global",
    )
    parser.add_argument(
        "--global",
        dest="setup_global",
        action="store_true",
        help="Save configuration to user global settings (~/.coderai)",
    )
    parser.add_argument("--plan", action="store_true", help="start session in Plan Mode")
    parser.add_argument(
        "--max-subagent-depth",
        type=int,
        default=None,
        help="maximum sub-agent nesting depth (default 3; env CODERAI_MAX_SUBAGENT_DEPTH)",
    )
    parser.add_argument(
        "--subagent-timeout",
        type=float,
        default=None,
        help="sub-agent timeout in seconds (default 90; env CODERAI_SUBAGENT_TIMEOUT_SECONDS)",
    )
    parser.add_argument(
        "--workflow-max-agents",
        type=int,
        default=None,
        help="workflow total agent cap per run (default 1000; env CODERAI_WORKFLOW_MAX_TOTAL_AGENTS)",
    )
    parser.add_argument(
        "--workflow-max-concurrency",
        type=int,
        default=None,
        help="workflow concurrent agent slots (default min(16, cores-2); env CODERAI_WORKFLOW_MAX_CONCURRENT_AGENTS)",
    )
    parser.add_argument(
        "--ralph-max-rounds",
        type=int,
        default=None,
        help="Ralph verification round ceiling (default 256; env CODERAI_RALPH_MAX_ROUNDS)",
    )
    parser.add_argument(
        "--max-continuable-agents",
        type=int,
        default=None,
        help="maximum live continuable sub-agents per session (default 50; env CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION)",
    )
    parser.add_argument(
        "--max-running-jobs",
        type=int,
        default=None,
        help="maximum concurrent running background jobs per session (default 50; env CODERAI_MAX_RUNNING_JOBS_PER_SESSION)",
    )
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
        diff_preview = req.get("diff_preview")
        risk_level = req.get("risk_level") or "MODERATE RISK"

        # In Plan Mode, mutating scopes are strictly forced to prompt even with --yes
        is_forced_plan_scope = plan_mode and any(s in PLAN_MODE_FORCE_ASK_SCOPES for s in scopes)

        if yes and not is_forced_plan_scope:
            replies.append({"toolCallId": tool_call_id, "permission": "allow"})
            always_allows.extend(scopes)
            continue

        always_target = next((s for s in scopes if s in ALWAYS_ALLOWED_SCOPES), None)
        has_always = bool(always_target and not plan_mode)

        if console is not None and _RICH:
            scope_items = []
            for sc in scopes:
                color = get_scope_color(sc)
                scope_items.append(f"[{color}]{sc}[/] [dim]({describe_scope(sc)})[/]")
            scopes_str = ", ".join(scope_items) if scope_items else "[green]none[/]"

            if "CRITICAL" in risk_level:
                border_color = "bright_red"
                badge_style = "bold white on red"
                risk_icon = "🚨"
            elif "HIGH" in risk_level:
                border_color = "red"
                badge_style = "bold white on red"
                risk_icon = "⚠️"
            elif "MODERATE" in risk_level:
                border_color = "yellow"
                badge_style = "bold black on yellow"
                risk_icon = "⚡"
            else:
                border_color = "cyan"
                badge_style = "bold black on green"
                risk_icon = "🛡️"

            badge_str = f"[{badge_style}] {risk_icon} {risk_level} [/]"

            card_lines = []
            if command:
                card_lines.append(f"  [bold cyan]Action:[/]   [bold white]{name}[/]")
                card_lines.append(f"  [bold cyan]Command:[/]  [bold white]{command}[/]")
            else:
                card_lines.append(f"  [bold cyan]Action:[/]   [bold white]{name}[/]")

            if description:
                card_lines.append(f"  [dim italic]{description}[/]")

            if is_forced_plan_scope:
                card_lines.append(
                    "  [bold red]⚠️  Plan Mode Warning:[/] [yellow]Mutating action requested while in Plan Mode.[/]"
                )

            card_lines.append(f"  [dim]Scopes:[/]   {scopes_str}")

            panel = Panel(
                "\n".join(card_lines),
                title=f"[bold yellow]! Permission Required ({idx}/{len(requests)})[/]  {badge_str}",
                border_style=border_color,
                padding=(0, 1),
            )
            console.print()
            console.print(panel)

            if diff_preview and isinstance(diff_preview, str) and diff_preview.strip():
                render_diff_preview(console, diff_preview, title=f"Pre-Approval Diff ({name})")
        else:
            print(f"\n! Permission Required ({idx}/{len(requests)}) [{risk_level}]")
            print(f"  Action:      {name}")
            if command:
                print(f"  Command:     {command}")
            if description:
                print(f"  Description: {description}")
            if is_forced_plan_scope:
                print("  [WARNING] Mutating action requested while in Plan Mode.")
            if scopes:
                print(f"  Scopes:      {', '.join(scopes)}")
            if diff_preview and isinstance(diff_preview, str) and diff_preview.strip():
                render_diff_preview(None, diff_preview, title=f"Pre-Approval Diff ({name})")

        options: list[tuple[str, str, str]] = [("allow", "1", "Yes (allow action)")]
        if has_always and always_target:
            options.append(
                ("always", "2", f"Yes, and always allow {describe_scope(always_target)}")
            )
            options.append(("deny", "3", "No (deny action)"))
            extra_keys = []
            if command:
                options.append(("edit", "e", "Edit command before running"))
                extra_keys.append("e")
            if diff_preview and isinstance(diff_preview, str) and diff_preview.strip():
                options.append(("diff", "d", "View diff preview"))
                extra_keys.append("d")
            extra_str = f"/{'/'.join(extra_keys)}" if extra_keys else ""
            prompt_str = f"  Allow? [1/2/3] (or y/a/n{extra_str}): "
        else:
            options.append(("deny", "2", "No (deny action)"))
            extra_keys = []
            if command:
                options.append(("edit", "e", "Edit command before running"))
                extra_keys.append("e")
            if diff_preview and isinstance(diff_preview, str) and diff_preview.strip():
                options.append(("diff", "d", "View diff preview"))
                extra_keys.append("d")
            extra_str = f"/{'/'.join(extra_keys)}" if extra_keys else ""
            prompt_str = f"  Allow? [1/2] (or y/n{extra_str}): "

        if console is not None and _RICH:
            for _, key, label in options:
                if key == "1":
                    console.print(f"    [bold green]{key}.[/] [bold white]{label}[/] [dim](or 'y')[/]")
                elif key == "2" and has_always:
                    console.print(f"    [bold cyan]{key}.[/] [bold white]{label}[/] [dim](or 'a')[/]")
                elif key == "e":
                    console.print(f"    [bold yellow]{key}.[/] [bold white]{label}[/]")
                elif key == "d":
                    console.print(f"    [bold magenta]{key}.[/] [bold white]{label}[/]")
                else:
                    console.print(f"    [bold red]{key}.[/] [bold white]{label}[/] [dim](or 'n')[/]")
        else:
            print("  Options:")
            for _, key, label in options:
                print(f"    {key}. {label}")

        while True:
            try:
                raw_choice = input(prompt_str).strip().lower()
            except (EOFError, KeyboardInterrupt):
                _clear_task_cancellation()
                raw_choice = "n"

            if raw_choice in ("d", "diff") and diff_preview and isinstance(diff_preview, str):
                if console is not None and _RICH:
                    render_diff_preview(console, diff_preview, title=f"Pre-Approval Diff ({name})")
                else:
                    render_diff_preview(None, diff_preview, title=f"Pre-Approval Diff ({name})")
                continue

            if raw_choice in ("e", "edit") and command:
                try:
                    edited_cmd = input(f"  Edit command [{command}]: ").strip()
                    if edited_cmd:
                        req["command"] = edited_cmd
                        if isinstance(req.get("input"), dict) and "command" in req["input"]:
                            req["input"]["command"] = edited_cmd
                        if isinstance(req.get("arguments"), dict) and "command" in req["arguments"]:
                            req["arguments"]["command"] = edited_cmd
                        command = edited_cmd
                    replies.append({"toolCallId": tool_call_id, "permission": "allow", "command": command})
                    break
                except (EOFError, KeyboardInterrupt):
                    _clear_task_cancellation()
                    continue

            if has_always and always_target and raw_choice in ("2", "a", "always"):
                replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                always_allows.append(always_target)
                break
            elif raw_choice in ("n", "no", "deny", "3") or (
                raw_choice == "2" and not has_always
            ):
                replies.append({"toolCallId": tool_call_id, "permission": "deny"})
                break
            else:
                replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                break

    return replies, always_allows


def _prompt_user_questions(questions: list[dict[str, Any]]) -> str:
    """Prompt the user interactively with arrow-key navigation and custom text when an AskUserQuestion tool execution occurs."""
    answers: list[str] = []

    for idx, item in enumerate(questions, 1):
        q_text = item.get("question", "")
        options = item.get("options") or []
        multi_select = bool(item.get("multiSelect", False))

        if not options:
            if console is not None and _RICH:
                console.print(
                    f"\n  [bold yellow]? Question {idx}/{len(questions)}:[/] [bold white]{q_text}[/]"
                )
            else:
                print(f"\n? Question {idx}/{len(questions)}: {q_text}")
            try:
                final_ans = input("  Your answer: ").strip()
            except (EOFError, KeyboardInterrupt):
                _clear_task_cancellation()
                final_ans = ""
            if final_ans:
                answers.append(f"{q_text}: {final_ans}")
            continue

        items: list[tuple[str, str, str]] = []
        for opt in options:
            label = opt.get("label", "")
            desc = opt.get("description", "")
            items.append((label, label, desc))

        title = f"Question {idx}/{len(questions)}: {q_text}"
        if multi_select:
            title += " (multi-select)"

        res = select_with_arrows(
            console,
            items,
            title=title,
            default_idx=0,
            allow_custom=True,
        )

        final_ans = ""
        if isinstance(res, int) and 0 <= res < len(options):
            final_ans = options[res].get("label", "")
        elif isinstance(res, str) and res.strip():
            raw_str = res.strip()
            if multi_select and "," in raw_str:
                tokens = [t.strip() for t in raw_str.split(",")]
                selected_labels: list[str] = []
                for tok in tokens:
                    if tok.isdigit() and 1 <= int(tok) <= len(options):
                        selected_labels.append(options[int(tok) - 1].get("label", ""))
                    elif tok:
                        selected_labels.append(tok)
                final_ans = ", ".join(selected_labels) if selected_labels else raw_str
            elif raw_str.isdigit() and 1 <= int(raw_str) <= len(options):
                final_ans = options[int(raw_str) - 1].get("label", "")
            else:
                final_ans = raw_str
        elif options:
            final_ans = options[0].get("label", "")

        if final_ans:
            answers.append(f"{q_text}: {final_ans}")

    return "\n".join(answers) if answers else "User responded."


class _StreamState:
    """Track streaming progress, live reasoning tokens, and execution spinners."""

    def __init__(self) -> None:
        self.streamed_content: list[str] = []
        self.is_streaming: bool = False
        self.thinking_streamer = LiveThinkingStreamer(console)
        self.active_status_spinner: Any | None = None
        self.thinking_rendered: bool = False

    def reset(self) -> None:
        self.streamed_content.clear()
        self.is_streaming = False
        self.thinking_streamer.reset()
        self.stop_spinner()
        self.thinking_rendered = False

    def on_thinking_chunk(self, chunk: str) -> None:
        self.stop_spinner()
        self.thinking_streamer.on_chunk(chunk)

    def on_chunk(self, chunk: str) -> None:
        if chunk:
            if self.thinking_streamer.is_active:
                self.thinking_streamer.finalize(console, expanded=_THINKING_EXPANDED)
                self.thinking_rendered = True
            self.stop_spinner()
            self.streamed_content.append(chunk)
            self.is_streaming = True
            sys.stdout.write(chunk)
            sys.stdout.flush()

    def start_spinner(self, message: str) -> None:
        if console is not None and _RICH and hasattr(console, "status"):
            self.stop_spinner()
            try:
                self.active_status_spinner = console.status(
                    f"[bold cyan]{message}[/]", spinner="dots"
                )
                self.active_status_spinner.start()
            except Exception:
                self.active_status_spinner = None

    def stop_spinner(self) -> None:
        if self.active_status_spinner is not None:
            try:
                self.active_status_spinner.stop()
            except Exception:
                pass
            self.active_status_spinner = None

    def had_streamed(self) -> bool:
        return bool(self.streamed_content)

    def ensure_newline(self) -> bool:
        """Ensure stream cursor is on a fresh line before printing banners or cards."""
        self.stop_spinner()
        if self.thinking_streamer.is_active:
            self.thinking_streamer.finalize(console, expanded=_THINKING_EXPANDED)
            self.thinking_rendered = True
        if self.had_streamed():
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.streamed_content.clear()
            self.is_streaming = False
            return True
        return False


_STREAM_STATE = _StreamState()


def _on_assistant_message(message: SessionMessage, should_connect: bool) -> None:
    """Format and render assistant messages, thinking blocks, and tool executions."""
    _STREAM_STATE.stop_spinner()
    was_streamed = _STREAM_STATE.ensure_newline()

    meta = message.meta or {}
    if meta.get("asThinking"):
        render_thinking_block(console, message.content, expanded=_THINKING_EXPANDED)
        return

    if message.role == "tool":
        render_tool_card(console, message)
        return

    if message.thinking and not _STREAM_STATE.thinking_rendered:
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
                console.print(f"  [dim]→ invoking[/] [bold cyan]{name}[/][dim]...[/]")
                _STREAM_STATE.start_spinner(f"Executing {name}...")
            else:
                print(f"  → invoking {name}...")


async def _drain_pending_interactions(mgr: SessionManager, session_id: str, yes: bool) -> None:
    """Drain permissions and interactive user questions until session reaches a stable state."""
    while True:
        entry = mgr.get_session(session_id)
        if entry is None:
            return

        if entry.status == "ask_permission":
            _STREAM_STATE.stop_spinner()
            _STREAM_STATE.ensure_newline()
            replies, always = _prompt_permissions(
                entry.ask_permissions or [], yes, plan_mode=bool(entry.plan_mode)
            )
            if always:
                append_project_permission_allows(mgr.project_root, always)
            _STREAM_STATE.reset()
            await mgr.reply_session(session_id, None, permission_replies=replies)
            continue

        if entry.status in ("ask_user_question", "waiting_for_user"):
            _STREAM_STATE.stop_spinner()
            _STREAM_STATE.ensure_newline()
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


def _render_help_menu(cmd_name: str | None = None) -> None:
    """Display interactive command help or specific command contextual help."""
    render_help(cmd_name, console if _RICH else None)


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
            raw_target = fork.strip()
            resolved = mgr.resolve_session_id(raw_target)
            target_id = resolved or raw_target
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
            raw_id = resume.strip()
            resolved_id = mgr.resolve_session_id(raw_id)
            if resolved_id is None:
                all_sessions = mgr.list_sessions()
                if all_sessions:
                    if console is not None and _RICH:
                        console.print(
                            f"[bold red]No saved session with id, prefix, or checkpoint '[bold white]{raw_id}[/bold white]'.[/]"
                        )
                        console.print(
                            f"[dim]Run [bold cyan]coderai --resume[/bold cyan] to browse and select from {len(all_sessions)} saved session(s).[/dim]\n"
                        )
                    else:
                        print(
                            f"No saved session with id, prefix, or checkpoint '{raw_id}'. Run 'coderai --resume' to browse saved sessions."
                        )
                else:
                    if console is not None and _RICH:
                        console.print(
                            "[yellow]No saved sessions found in this workspace directory.[/]"
                        )
                    else:
                        print("No saved sessions found in this workspace directory.")
                return 1
            session_id = resolved_id

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
        reasoning_effort=mgr.get_reasoning_effort(),
    )

    # Check if active model has a configured API key
    active_m = mgr.get_active_model()
    resolved_settings = mgr.get_resolved_settings()
    from coderai.core.openai_client import resolve_model_provider_routing

    _, active_key = resolve_model_provider_routing(
        active_m,
        explicit_base_url=resolved_settings.get("baseURL"),
        explicit_api_key=resolved_settings.get("apiKey"),
    )
    if not active_key:
        if console is not None and _RICH:
            console.print(
                f"  [bold yellow]! No API key configured for active model '[bold white]{active_m}[/bold white]'.[/] "
                f"[dim]Run [bold cyan]/setup[/bold cyan] to configure keys & providers.[/dim]\n"
            )
        else:
            print(
                f"! No API key configured for active model '{active_m}'. Run /setup to configure keys & providers.\n"
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
                prompt_label = "[plan] ❯ " if active_plan_mode else "❯ "
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
                cmd, cmd_arg = parse_slash_command(raw)

                if cmd in ("/exit", "/quit"):
                    break

                if cmd in ("/help", "/?"):
                    _render_help_menu(cmd_arg if cmd_arg else None)
                    continue

                if cmd == "/clear":
                    os.system("cls" if os.name == "nt" else "clear")
                    continue

                if cmd in ("/setup", "/auth", "/keys", "/configure"):
                    from coderai.cli.setup_wizard import run_setup_wizard

                    run_setup_wizard(
                        console,
                        project_root=mgr.project_root,
                        mgr=mgr,
                        initial_subcommand=cmd_arg if cmd_arg else None,
                    )
                    continue

                if cmd == "/doctor":
                    from coderai.cli.doctor import run_doctor_diagnostics, render_doctor

                    report = run_doctor_diagnostics(mgr.project_root, mgr)
                    render_doctor(console, report)
                    continue

                if cmd in ("/jobs", "/job"):
                    job_store = getattr(mgr, "job_store", None)
                    if not job_store:
                        print("Job store subsystem is not initialized.")
                        continue
                    tokens_sub = cmd_arg.split(None, 1)
                    action = tokens_sub[0].lower() if tokens_sub else "list"
                    job_target = tokens_sub[1].strip() if len(tokens_sub) > 1 else ""
                    if action in ("", "list"):
                        jobs = [
                            j
                            for j in getattr(job_store, "_jobs", {}).values()
                            if not session_id
                            or j.session_id == session_id
                            or j.session_id == "default"
                        ]
                        if not jobs:
                            print("No background jobs recorded in active session.")
                        else:
                            if console is not None and _RICH and Table is not None:
                                jt = Table(title="Session Background Jobs", border_style="cyan")
                                jt.add_column("Status", width=12)
                                jt.add_column("Job ID", style="bold cyan", width=14)
                                jt.add_column("Kind", style="magenta", width=10)
                                jt.add_column("Command / Label", style="white")
                                for j in jobs:
                                    status_color = (
                                        "green"
                                        if j.status == "completed"
                                        else ("yellow" if j.status == "running" else "red")
                                    )
                                    jt.add_row(
                                        f"[{status_color}]{j.status.upper()}[/]",
                                        j.id,
                                        j.kind,
                                        j.label[:60],
                                    )
                                console.print(jt)
                            else:
                                for j in jobs:
                                    print(
                                        f"[{j.status.upper():9}] {j.id:12} {j.kind:8} {j.label[:60]}"
                                    )
                    elif action == "kill" and job_target:
                        res = job_store.cancel(job_target)
                        print(
                            f"Cancelled job {job_target}"
                            if res
                            else f"Job '{job_target}' not found or already stopped."
                        )
                    elif action == "logs" and job_target:
                        j = getattr(job_store, "_jobs", {}).get(job_target)
                        if j and j.output_path and os.path.exists(j.output_path):
                            with open(j.output_path, "r", encoding="utf-8", errors="replace") as f:
                                lines = f.readlines()
                            print(f"--- Job {job_target} Logs ---\n" + "".join(lines[-30:]))
                        else:
                            print(f"No log output available for job '{job_target}'.")
                    else:
                        print("Usage: /jobs or /job [list|kill <id>|logs <id>]")
                    continue

                if cmd == "/schedule":
                    sched_mgr = getattr(mgr, "schedule_manager", None)
                    if not sched_mgr:
                        print("Schedule manager subsystem is not initialized.")
                        continue
                    tokens_sub = cmd_arg.split(None, 2)
                    action = tokens_sub[0].lower() if tokens_sub else "list"
                    if action in ("", "list"):
                        records = list(getattr(sched_mgr, "_schedules", {}).values())
                        if not records:
                            print("No scheduled timers or reminders in workspace.")
                        else:
                            if console is not None and _RICH and Table is not None:
                                st = Table(
                                    title="Scheduled Timers & Reminders", border_style="cyan"
                                )
                                st.add_column("ID", style="bold cyan", width=6)
                                st.add_column("State", width=12)
                                st.add_column("Kind", style="magenta", width=8)
                                st.add_column("Scheduled At", style="dim", width=22)
                                st.add_column("Prompt / Instruction", style="white")
                                for r in records:
                                    state_color = (
                                        "green"
                                        if r.state == "dispatched"
                                        else ("yellow" if r.state == "scheduled" else "dim")
                                    )
                                    st.add_row(
                                        r.id,
                                        f"[{state_color}]{r.state}[/]",
                                        r.kind,
                                        r.scheduled_at[:19],
                                        r.prompt[:50],
                                    )
                                console.print(st)
                            else:
                                for r in records:
                                    print(
                                        f"[{r.state:10}] ID:{r.id:4} Kind:{r.kind:6} At:{r.scheduled_at[:19]} -> {r.prompt[:50]}"
                                    )
                    elif action == "after" and len(tokens_sub) >= 3 and tokens_sub[1].isdigit():
                        sec = int(tokens_sub[1])
                        p = tokens_sub[2]
                        rec = sched_mgr.create(prompt=p, after_seconds=sec, session_id=session_id)
                        print(f"✓ Scheduled reminder #{rec.id} in {sec}s: {p}")
                    elif action == "every" and len(tokens_sub) >= 3 and tokens_sub[1].isdigit():
                        sec = int(tokens_sub[1])
                        p = tokens_sub[2]
                        rec = sched_mgr.create(prompt=p, every_seconds=sec, session_id=session_id)
                        print(f"✓ Scheduled recurring reminder #{rec.id} every {sec}s: {p}")
                    elif action in ("cancel", "rm", "delete") and len(tokens_sub) >= 2:
                        cid = tokens_sub[1]
                        res = sched_mgr.delete(cid)
                        print(
                            f"✓ Cancelled schedule #{cid}" if res else f"Schedule #{cid} not found."
                        )
                    else:
                        print(
                            "Usage: /schedule [list|after <sec> <prompt>|every <sec> <prompt>|cancel <id>]"
                        )
                    continue

                if cmd in ("/agents", "/subagents"):
                    agent_reg = getattr(mgr, "agent_registry", None)
                    if not agent_reg:
                        print("Agent registry subsystem is not initialized.")
                        continue
                    tokens_sub = cmd_arg.split(None, 2)
                    action = tokens_sub[0].lower() if tokens_sub else "list"
                    if action in ("", "list"):
                        agents = agent_reg.list(session_id)
                        if not agents:
                            print("No subagents registered in active session.")
                        else:
                            if console is not None and _RICH and Table is not None:
                                at = Table(title="Session Subagents", border_style="cyan")
                                at.add_column("Agent ID", style="bold cyan", width=14)
                                at.add_column("Status", width=12)
                                at.add_column("Mode", style="magenta", width=10)
                                at.add_column("Depth", width=6)
                                at.add_column("Inbox", width=6)
                                at.add_column("Task Description", style="white")
                                for a in agents:
                                    status_color = (
                                        "green"
                                        if a.status == "completed"
                                        else ("yellow" if a.status == "running" else "red")
                                    )
                                    at.add_row(
                                        a.id[:12],
                                        f"[{status_color}]{a.status}[/]",
                                        a.mode,
                                        str(a.depth),
                                        str(len(a.inbox)),
                                        a.description[:50],
                                    )
                                console.print(at)
                            else:
                                for a in agents:
                                    print(
                                        f"[{a.status.upper():11}] ID:{a.id:12} Mode:{a.mode:8} Depth:{a.depth} Inbox:{len(a.inbox)} -> {a.description[:50]}"
                                    )
                    elif action == "tree":
                        agents = agent_reg.list(session_id)
                        roots = [
                            a
                            for a in agents
                            if not a.parent_agent_id or a.parent_agent_id not in agent_reg._agents
                        ]
                        if not roots:
                            print("No subagent hierarchy tree in active session.")
                        else:
                            for r in roots:
                                tree = agent_reg.get_tree(r.id)
                                print(json.dumps(tree, indent=2))
                    elif action == "report" and len(tokens_sub) >= 2:
                        aid = tokens_sub[1]
                        a = agent_reg.get(aid)
                        if a and a.report:
                            print(f"--- Subagent {aid} Report ---\n{a.report}")
                        elif a:
                            print(f"Subagent '{aid}' has status '{a.status}' with no final report.")
                        else:
                            print(f"Unknown subagent ID '{aid}'.")
                    elif action == "send" and len(tokens_sub) >= 3:
                        aid = tokens_sub[1]
                        msg = tokens_sub[2]
                        res = agent_reg.send(aid, msg)
                        print(
                            f"✓ Message dispatched to subagent '{aid}' inbox."
                            if res
                            else f"Unknown subagent ID '{aid}'."
                        )
                    else:
                        print("Usage: /agents [list|tree|report <id>|send <id> <msg>]")
                    continue

                if cmd == "/teams":
                    team_mgr = getattr(mgr, "team_manager", None)
                    if not team_mgr:
                        from coderai.core.teams.manager import TeamManager

                        team_mgr = TeamManager()
                    teammates = team_mgr.list_teammates()
                    configured_teams = mgr.get_resolved_settings().get("teams") or []
                    if not teammates and not configured_teams:
                        print("No active agent teammates or teams configured in workspace.")
                    else:
                        if console is not None and _RICH and Table is not None:
                            tt = Table(title="Agent Teams & Teammates", border_style="cyan")
                            tt.add_column("Teammate ID", style="bold cyan", width=16)
                            tt.add_column("Name", style="magenta", width=18)
                            tt.add_column("Role", style="white", width=18)
                            tt.add_column("Mode", width=12)
                            tt.add_column("Status", width=12)
                            for tm in teammates:
                                status_color = (
                                    "green"
                                    if tm.status in ("idle", "ready")
                                    else ("yellow" if tm.status == "working" else "dim")
                                )
                                tt.add_row(
                                    tm.teammate_id,
                                    tm.name,
                                    tm.role,
                                    tm.mode,
                                    f"[{status_color}]{tm.status}[/]",
                                )
                            console.print(tt)
                        else:
                            for tm in teammates:
                                print(
                                    f"[{tm.status.upper():8}] ID:{tm.teammate_id:12} {tm.name:16} ({tm.role}) [{tm.mode}]"
                                )
                    continue

                if cmd == "/lsp":
                    import shutil
                    from coderai.core.lsp.client import LSP_SERVER_COMMANDS, get_lsp_client

                    lsp_client = get_lsp_client(mgr.project_root)
                    detected_servers = []
                    for ext, cmd_args in LSP_SERVER_COMMANDS.items():
                        bin_name = cmd_args[0]
                        found_path = shutil.which(bin_name)
                        if not found_path and ext == ".py":
                            found_path = shutil.which("pylsp")
                            if found_path:
                                bin_name = "pylsp"
                        detected_servers.append(
                            (
                                ext,
                                bin_name,
                                bool(found_path),
                                found_path or "Not installed (AST static fallback active)",
                            )
                        )

                    active_inst_count = len(lsp_client._instances)
                    if console is not None and _RICH and Table is not None:
                        lt = Table(
                            title=f"LSP Status & Language Servers ({active_inst_count} active instance{'s' if active_inst_count != 1 else ''})",
                            border_style="cyan",
                        )
                        lt.add_column("Extension", style="bold cyan", width=12)
                        lt.add_column("Server Binary", style="magenta", width=26)
                        lt.add_column("Status", width=14)
                        lt.add_column("Binary Path / Fallback", style="dim")
                        for ext, bin_name, available, path_info in detected_servers:
                            status_badge = (
                                "[green]AVAILABLE[/]" if available else "[yellow]STATIC AST[/]"
                            )
                            lt.add_row(ext, bin_name, status_badge, path_info)
                        console.print(lt)
                    else:
                        print(f"\n--- LSP Language Servers ({active_inst_count} active) ---")
                        for ext, bin_name, available, path_info in detected_servers:
                            status_text = "AVAILABLE" if available else "STATIC AST"
                            print(f"  {ext:10} {bin_name:26} [{status_text:10}] {path_info}")
                        print()
                    continue

                if cmd == "/rename":
                    if not cmd_arg:
                        print("Usage: /rename <new_title> or /rename <session_id> <new_title>")
                        continue
                    parts = cmd_arg.split(None, 1)
                    target_sid = session_id
                    new_title = cmd_arg
                    if len(parts) > 1 and mgr.get_session(parts[0]):
                        target_sid = parts[0]
                        new_title = parts[1]
                    if not target_sid:
                        print(
                            "No active session to rename. Usage: /rename <session_id> <new_title>"
                        )
                        continue
                    if not new_title:
                        print("Usage: /rename <new_title>")
                        continue
                    if mgr.rename_session(target_sid, new_title):
                        if console is not None and _RICH:
                            console.print(f"[bold green]✓ Renamed session to:[/] {new_title}")
                        else:
                            print(f"✓ Renamed session to: {new_title}")
                    else:
                        print(f"Failed to rename session '{target_sid}'.")
                    continue

                if cmd == "/image":
                    from coderai.cli.image_attachment import parse_and_attach_image

                    tokens_img = cmd_arg.split(None, 1)
                    if not tokens_img:
                        print("Usage: /image <file_path> [prompt]")
                        continue
                    img_path = tokens_img[0]
                    img_prompt = (
                        tokens_img[1]
                        if len(tokens_img) > 1
                        else f"Inspect and analyze image: {img_path}"
                    )
                    content_param, err = parse_and_attach_image(img_path, mgr.project_root)
                    if err:
                        print(f"Image error: {err}")
                        continue
                    if content_param is None:
                        print("Image error: attachment metadata was not produced")
                        continue
                    if console is not None and _RICH:
                        console.print(
                            f"[bold green]✓ Attached image:[/] [cyan]{content_param['name']}[/] "
                            f"({content_param['width']}x{content_param['height']} • {content_param['bytes'] / 1024:.1f} KB)"
                        )
                    else:
                        print(
                            f"✓ Attached image: {content_param['name']} "
                            f"({content_param['width']}x{content_param['height']})"
                        )

                    _STREAM_STATE.reset()

                    async def _run_image_turn() -> str | None:
                        nonlocal session_id
                        if session_id is None:
                            s_id = await mgr.create_session(img_prompt, plan_mode=active_plan_mode)
                            msgs = mgr.list_session_messages(s_id)
                            if msgs:
                                user_msg = next(
                                    (m for m in reversed(msgs) if m.role == "user"), None
                                )
                                if user_msg:
                                    user_msg.meta = {
                                        **(user_msg.meta or {}),
                                        "contentParams": [content_param],
                                    }
                        else:
                            s_id = session_id
                            user_msg = mgr._build_message(
                                session_id,
                                "user",
                                img_prompt,
                                meta={"contentParams": [content_param]},
                            )
                            mgr._append_message(user_msg)
                            await mgr.reply_session(session_id, plan_mode=active_plan_mode)
                        await _drain_pending_interactions(mgr, s_id, yes)
                        return s_id

                    active_turn_task = asyncio.create_task(_run_image_turn())
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

                if cmd in ("/editor", "/edit"):
                    from coderai.cli.input_engine import open_external_editor

                    initial_draft = cmd_arg if cmd_arg else ""
                    composed = open_external_editor(initial_draft)
                    if not composed:
                        print("Editor closed with empty content; prompt cancelled.")
                        continue
                    if console is not None and _RICH:
                        console.print(
                            f"[bold cyan]Prompt submitted from editor ({len(composed)} chars)[/]"
                        )
                    else:
                        print(f"Prompt submitted from editor ({len(composed)} chars)")
                    raw = composed
                    # Fallthrough to normal prompt processing below!

                elif cmd == "/paste":
                    from coderai.cli.input_engine import read_paste_mode

                    composed = read_paste_mode()
                    if not composed:
                        print("Paste buffer empty; prompt cancelled.")
                        continue
                    raw = composed
                    # Fallthrough to normal prompt processing below!

                else:
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
                        from coderai.core.settings import (
                            read_project_settings,
                            write_project_settings,
                        )

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
                        write_project_settings(settings, mgr.project_root)
                        print(
                            f"Permission preset set to {parsed}. New sessions will use this preset."
                        )
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
                            print(f"Added goal {goal.id}: {goal.objective}")
                        elif action in ("done", "cancel", "start") and rest:
                            updated = store.update(
                                sid,
                                rest,
                                status={
                                    "done": "done",
                                    "cancel": "cancelled",
                                    "start": "in_progress",
                                }[action],
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
                                    print(
                                        f"Failed to reconnect MCP server '{server_name}'{err_msg}"
                                    )
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

                    if cmd in ("/effort", "/reasoning"):
                        if not cmd_arg:
                            from coderai.cli.interactive_menu import (
                                select_reasoning_effort_interactive,
                            )

                            chosen_effort = select_reasoning_effort_interactive(
                                console, mgr.get_reasoning_effort(), model=mgr.get_active_model()
                            )
                            mgr.set_reasoning_effort(chosen_effort)
                            if console is not None and _RICH:
                                console.print(
                                    f"[bold green]Switched reasoning effort to:[/] [bold magenta]{chosen_effort}[/]"
                                )
                            else:
                                print(f"Switched reasoning effort to: {chosen_effort}")
                        else:
                            from coderai.core.common.openai_thinking import (
                                normalize_reasoning_effort,
                            )

                            target_effort = normalize_reasoning_effort(cmd_arg.strip())
                            mgr.set_reasoning_effort(target_effort)
                            if console is not None and _RICH:
                                console.print(
                                    f"[bold green]Switched reasoning effort to:[/] [bold magenta]{target_effort}[/]"
                                )
                            else:
                                print(f"Switched reasoning effort to: {target_effort}")
                        continue

                    if cmd == "/sessions":
                        all_sessions = mgr.list_sessions()
                        chosen_action = select_session_interactive(console, all_sessions)
                        if chosen_action:
                            if chosen_action.startswith("delete:"):
                                del_target = chosen_action.split(":", 1)[1]
                                if mgr.delete_session(del_target):
                                    if session_id == del_target:
                                        session_id = None
                                    if console is not None and _RICH:
                                        console.print(
                                            f"[bold green]✓ Deleted session:[/] {del_target}"
                                        )
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
                                await mgr.reply_session(
                                    session_id, "/init", plan_mode=active_plan_mode
                                )
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
                        target_id = cmd_arg.strip()
                        resumed = mgr.get_session(target_id)
                        if resumed is None:
                            print(f"No saved session with id '{target_id}'.")
                            session_id = None
                        else:
                            active_plan_mode = resumed.plan_mode
                            session_id = resumed.id
                            if console is not None and _RICH:
                                console.print(f"[bold green]Resumed session:[/] {session_id}")
                            else:
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

    # Forward orchestration flags to the CODERAI_* environment contract so the
    # shared orchestration layer (and child processes) resolve one source.
    for flag, env_name in (
        ("max_subagent_depth", "CODERAI_MAX_SUBAGENT_DEPTH"),
        ("subagent_timeout", "CODERAI_SUBAGENT_TIMEOUT_SECONDS"),
        ("workflow_max_agents", "CODERAI_WORKFLOW_MAX_TOTAL_AGENTS"),
        ("workflow_max_concurrency", "CODERAI_WORKFLOW_MAX_CONCURRENT_AGENTS"),
        ("ralph_max_rounds", "CODERAI_RALPH_MAX_ROUNDS"),
        ("max_continuable_agents", "CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION"),
        ("max_running_jobs", "CODERAI_MAX_RUNNING_JOBS_PER_SESSION"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            os.environ[env_name] = str(value)

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
        else (exec_str if exec_str else (" ".join(args.prompt) if has_positional else None))
    )

    # Check if CLI invocation is setup, config, or provider key management
    is_setup_cmd = (
        getattr(args, "setup", False)
        or bool(getattr(args, "setup_provider", None))
        or bool(getattr(args, "setup_key", None))
        or bool(getattr(args, "setup_base_url", None))
        or bool(getattr(args, "setup_model", None))
        or bool(getattr(args, "setup_test", False))
        or bool(getattr(args, "setup_status", False))
        or (
            prompt_value in ("setup", "configure", "auth", "keys")
            and not has_exec
            and not args.resume
            and not args.fork
            and not has_prompt_flag
        )
    )
    if is_setup_cmd:
        from coderai.cli.setup_wizard import run_setup_cli

        return run_setup_cli(args, project_root=project_root)

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

    # Explicit presets take precedence; new prompt runs default to core.
    preset_mode = args.preset
    if prompt_value and not (args.resume or args.fork or args.last) and not preset_mode:
        preset_mode = "core"

    async def _main() -> int:
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
                preset=preset_mode or "core",
            )

        mgr = build_session_manager(
            project_root,
            model=args.model,
            preset=preset_mode,
            on_assistant_message=_on_assistant_message,
            on_stream_chunk=_STREAM_STATE.on_chunk,
            on_thinking_chunk=_STREAM_STATE.on_thinking_chunk,
        )
        await mgr.init_mcp_servers()
        try:
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
            await close_session_manager(mgr)

    try:
        return asyncio.run(_main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
