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

from coderai.cli.term import ensure_new_line, ensure_tty_sane

_RICH = True
# Phase0: neutral-themed console with MANPAGER-safe pager (Kimi parity: ui/shell/console.py:63)
console: Any
try:
    from coderai.cli.console import console as _kimi_console  # type: ignore[assignment]

    console = _kimi_console
except Exception:
    console = Console()


def error_callout(console: Any | None, title: str, detail: str, hint: str = "") -> None:
    """Standardized error panel (Kimi parity): red border, actionable hint."""
    msg = f"[bold red]✗ {title}[/]\n[white]{detail}[/]"
    if hint:
        msg += f"\n[dim]→ {hint}[/]"
    eff = console if console is not None else globals().get("console")
    if eff is not None and _RICH:
        from rich.panel import Panel

        eff.print(Panel(msg, border_style="red", padding=(0, 1)))
    else:
        print(f"Error: {title} — {detail} {hint}".strip())


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

        # Phase4: try ApprovalRequestPanel when TTY+Rich, else fallback (keeps tests green when isatty==False)
        use_panel = bool(console is not None and _RICH and sys.stdin.isatty())
        if use_panel:
            try:
                from coderai.cli.approval_panel import ApprovalRequestPanel, show_approval_in_pager

                panel = ApprovalRequestPanel(req)
                console.print()
                console.print(panel.render())
                # Build prompt mirroring Kimi → [1] Approve once cyan, etc.
                has_always_panel = any(v == "approve_for_session" for _, v in panel.options)
                # reuse same prompt strings but map to panel indices
                if has_always_panel:
                    prompt_str = "  Allow? [y/a/n/e/d] (1/2/3, ctrl-e expand): "
                else:
                    prompt_str = "  Allow? [y/n/e/d] [1/2] (ctrl-e expand): "
                # ponytail: input() loop with Ctrl-E pager; Live pause/resume delegated to app Live if active
                # (global lock shim: we just Stop Live if _STREAM_STATE Live exists)
                while True:
                    try:
                        raw_choice = input(prompt_str).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        _clear_task_cancellation()
                        raw_choice = "n"
                    # Ctrl-E is \x05 when read via input in raw mode; handle both "ctrl-e" string and byte
                    if raw_choice in ("\x05", "ctrl-e", "expand") and panel.has_expandable_content:
                        # Live.stop→pager→start shape reset (Kimi _live_view.py:204)
                        live = getattr(_STREAM_STATE, "_live_ref", None)
                        if live is not None:
                            try:
                                from coderai.cli.progress import _reset_live_shape

                                live.stop()
                                show_approval_in_pager(panel)
                                _reset_live_shape(live)
                                live.start()
                                live.refresh()
                            except Exception:
                                show_approval_in_pager(panel)
                        else:
                            show_approval_in_pager(panel)
                        console.print(panel.render())
                        continue
                    if (
                        raw_choice in ("d", "diff")
                        and diff_preview
                        and isinstance(diff_preview, str)
                    ):
                        # d also expands via pager for parity
                        try:
                            show_approval_in_pager(panel)
                        except Exception:
                            render_diff_preview(
                                console, diff_preview, title=f"Pre-Approval Diff ({name})"
                            )
                        console.print(panel.render())
                        continue
                    if raw_choice in ("e", "edit") and command:
                        try:
                            edited_cmd = input(f"  Edit command [{command}]: ").strip()
                            if edited_cmd:
                                req["command"] = edited_cmd
                                if isinstance(req.get("input"), dict) and "command" in req["input"]:
                                    req["input"]["command"] = edited_cmd
                                if (
                                    isinstance(req.get("arguments"), dict)
                                    and "command" in req["arguments"]
                                ):
                                    req["arguments"]["command"] = edited_cmd
                                command = edited_cmd
                            replies.append(
                                {
                                    "toolCallId": tool_call_id,
                                    "permission": "allow",
                                    "command": command,
                                }
                            )
                            break
                        except (EOFError, KeyboardInterrupt):
                            _clear_task_cancellation()
                            continue
                    if has_always_panel and raw_choice in ("a", "always", "2"):
                        replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                        always_allows.append(always_target)  # type: ignore[arg-type]
                        break
                    elif raw_choice in ("n", "no", "deny", "3") or (
                        raw_choice == "2" and not has_always_panel
                    ):
                        # also handle numeric mapping via panel indices
                        if raw_choice == "3" and len(panel.options) >= 4:
                            # feedback option needs text
                            try:
                                fb = input(
                                    "  Feedback for model (Enter to skip, empty = plain reject): "
                                ).strip()
                            except (EOFError, KeyboardInterrupt):
                                fb = ""
                            if fb:
                                replies.append(
                                    {
                                        "toolCallId": tool_call_id,
                                        "permission": "deny",
                                        "feedback": fb,
                                    }
                                )
                            else:
                                replies.append({"toolCallId": tool_call_id, "permission": "deny"})
                        else:
                            replies.append({"toolCallId": tool_call_id, "permission": "deny"})
                        break
                    elif raw_choice in ("y", "yes", "1", "allow", ""):
                        replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                        break
                    elif raw_choice in ("4",):
                        # feedback option
                        try:
                            fb = input("  Feedback for model: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            fb = ""
                        replies.append(
                            {"toolCallId": tool_call_id, "permission": "deny", "feedback": fb}
                        )
                        break
                    else:
                        # numeric fallback via panel index
                        if raw_choice.isdigit() and 1 <= int(raw_choice) <= len(panel.options):
                            idx_sel = int(raw_choice) - 1
                            panel.selected_index = idx_sel
                            if panel.is_feedback_selected:
                                try:
                                    fb = input("  Feedback for model: ").strip()
                                except (EOFError, KeyboardInterrupt):
                                    fb = ""
                                replies.append(
                                    {
                                        "toolCallId": tool_call_id,
                                        "permission": "deny",
                                        "feedback": fb,
                                    }
                                )
                            elif panel.get_selected_response() == "approve_for_session":
                                replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                                always_allows.append(always_target)  # type: ignore[arg-type]
                            elif panel.get_selected_response() == "approve":
                                replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                            else:
                                replies.append({"toolCallId": tool_call_id, "permission": "deny"})
                            break
                        replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                        break
                continue
            except Exception:
                # fall through to fallback rendering on panel error
                pass

        # Fallback rendering (also used when isatty==False for tests)
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

            rich_panel = Panel(
                "\n".join(card_lines),
                title=f"[bold yellow]! Permission Required ({idx}/{len(requests)})[/]  {badge_str}",
                border_style=border_color,
                padding=(0, 1),
            )
            console.print()
            console.print(rich_panel)

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

        # Granular [y/n/a] style prompt (Kimi parity) — numeric aliases 1/2/3 kept for compat
        options: list[tuple[str, str, str]] = [("allow", "y", "Yes (allow once)")]
        if has_always and always_target:
            options.append(("always", "a", f"Yes, always allow {describe_scope(always_target)}"))
            options.append(("deny", "n", "No (deny action)"))
            extra_keys = []
            if command:
                options.append(("edit", "e", "Edit command before running"))
                extra_keys.append("e")
            if diff_preview and isinstance(diff_preview, str) and diff_preview.strip():
                options.append(("diff", "d", "View diff preview"))
                extra_keys.append("d")
            extra_str = f"/{'/'.join(extra_keys)}" if extra_keys else ""
            prompt_str = f"  Allow? [y/a/n{extra_str}] (1/2/3): "
        else:
            options.append(("deny", "n", "No (deny action)"))
            extra_keys = []
            if command:
                options.append(("edit", "e", "Edit command before running"))
                extra_keys.append("e")
            if diff_preview and isinstance(diff_preview, str) and diff_preview.strip():
                options.append(("diff", "d", "View diff preview"))
                extra_keys.append("d")
            extra_str = f"/{'/'.join(extra_keys)}" if extra_keys else ""
            prompt_str = f"  Allow? [y/n{extra_str}] [1/2]: "

        if console is not None and _RICH:
            for _, key, label in options:
                if key == "y":
                    console.print(f"    [bold green]{key}[/]  [bold white]{label}[/]")
                elif key == "a" and has_always:
                    console.print(f"    [bold cyan]{key}[/]  [bold white]{label}[/]")
                elif key == "e":
                    console.print(f"    [bold yellow]{key}[/]  [bold white]{label}[/]")
                elif key == "d":
                    console.print(f"    [bold magenta]{key}[/]  [bold white]{label}[/]")
                else:
                    console.print(f"    [bold red]{key}[/]  [bold white]{label}[/]")
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
                    replies.append(
                        {"toolCallId": tool_call_id, "permission": "allow", "command": command}
                    )
                    break
                except (EOFError, KeyboardInterrupt):
                    _clear_task_cancellation()
                    continue

            if has_always and always_target and raw_choice in ("a", "always", "2"):
                replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                always_allows.append(always_target)
                break
            elif raw_choice in ("n", "no", "deny", "3") or (raw_choice == "2" and not has_always):
                replies.append({"toolCallId": tool_call_id, "permission": "deny"})
                break
            elif raw_choice in ("y", "yes", "1", "allow", ""):
                replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                break
            else:
                replies.append({"toolCallId": tool_call_id, "permission": "allow"})
                break

    return replies, always_allows


def _prompt_user_questions(questions: list[dict[str, Any]]) -> str:
    """Prompt the user interactively — Phase4 QuestionRequestPanel tabs + Space multi-select."""
    # Fallback for tests (isatty==False) keeps original select_with_arrows string "1, 2" parse
    use_panel = bool(console is not None and _RICH and sys.stdin.isatty())
    if use_panel:
        try:
            from coderai.cli.question_panel import QuestionRequestPanel, show_question_body_in_pager

            panel = QuestionRequestPanel(questions)
            # ponytail: input() loop with tabs + Space toggle + _saved_selections
            # PTK KeyboardListener path would handle NUM_1..6/UP/DOWN/SPACE directly; here we
            # emulate via line input so manual validation shows tabs + Space hint.
            # Ceiling: full PTK key-level Space toggle needs Live+KeyboardListener; add when streaming modal needed.
            while True:
                console.print()
                console.print(panel.render())
                q = panel._current_question
                multi = bool(q.get("multiSelect"))
                opts = panel._options
                # hint already in panel; prompt for action
                if not q.get("options"):
                    try:
                        final_ans = input("  Your answer: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        _clear_task_cancellation()
                        final_ans = ""
                    if final_ans:
                        panel.submit_other(final_ans)
                    else:
                        panel.submit_other("")
                    if len(panel.get_answers()) >= len(questions):
                        break
                    continue
                # For multi-select, allow space-separated numbers
                prompt = "  Select"
                if len(questions) > 1:
                    prompt += f" (Q{panel._current_question_index + 1}/{len(questions)} — ←/→ tabs)"
                if multi:
                    prompt += " [e.g. 1,2 or 'space 1' to toggle, Enter submit, ctrl-e body]: "
                else:
                    prompt += " [1-{} or 'other <text>', ctrl-e body]: ".format(len(opts))
                try:
                    raw = input(prompt).strip()
                except (EOFError, KeyboardInterrupt):
                    _clear_task_cancellation()
                    raw = ""
                low = raw.lower()
                if low in ("ctrl-e", "\x05", "expand") and panel.has_expandable_content:
                    show_question_body_in_pager(panel)
                    continue
                if low in ("left", "prev", "p") and len(questions) > 1:
                    panel.prev_tab()
                    continue
                if low in ("right", "next", "n", "tab") and len(questions) > 1:
                    panel.next_tab()
                    continue
                if low in ("up", "k"):
                    panel.move_up()
                    continue
                if low in ("down", "j"):
                    panel.move_down()
                    continue
                if low.startswith("space "):
                    # space 1 -> toggle
                    parts = low.split()
                    for tok in parts[1:]:
                        if tok.isdigit() and 1 <= int(tok) <= len(opts):
                            panel.select_index(int(tok) - 1)
                            panel.toggle_select()
                    continue
                if low == "space" and multi:
                    panel.toggle_select()
                    continue
                if low in ("esc", "escape", "q"):
                    # dismiss
                    return (
                        "\n".join(f"{k}: {v}" for k, v in panel.get_answers().items())
                        if panel.get_answers()
                        else "User responded."
                    )
                if not raw and multi and panel._multi_selected:
                    # Enter to submit multi
                    if panel.submit():
                        break
                    continue
                if not raw and not multi:
                    # Enter submit single selected (unless Other)
                    if panel.is_other_selected:
                        try:
                            other_text = input("  Other value: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            other_text = ""
                        panel.submit_other(other_text)
                    else:
                        panel.submit()
                    if len(panel.get_answers()) >= len(questions):
                        break
                    continue
                # Parse comma-separated selections "1, 2" or single number
                tokens = [t.strip() for t in raw.split(",") if t.strip()]
                if len(tokens) == 1 and tokens[0].isdigit() and 1 <= int(tokens[0]) <= len(opts):
                    idx = int(tokens[0]) - 1
                    panel.select_index(idx)
                    if multi:
                        panel.toggle_select()
                        # keep in question until Enter
                        continue
                    else:
                        if panel.is_other_selected:
                            try:
                                other_text = input("  Other value: ").strip()
                            except (EOFError, KeyboardInterrupt):
                                other_text = ""
                            panel.submit_other(other_text)
                        else:
                            panel.submit()
                        if len(panel.get_answers()) >= len(questions):
                            break
                        continue
                if multi and "," in raw:
                    # "1,2,Other text" style
                    selected: list[str] = []
                    has_other = False
                    other_text = ""
                    for tok in tokens:
                        if tok.isdigit() and 1 <= int(tok) <= len(opts):
                            panel.select_index(int(tok) - 1)
                            panel.toggle_select()
                        elif tok.lower().startswith("other"):
                            has_other = True
                            other_text = tok[5:].strip().lstrip(":").strip()
                        elif tok:
                            selected.append(tok)
                    if has_other and not other_text:
                        try:
                            other_text = input("  Other value: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            other_text = ""
                    if panel.submit_other(other_text) if has_other else panel.submit():
                        if len(panel.get_answers()) >= len(questions):
                            break
                    continue
                # Fallback free text -> treat as Other
                if raw:
                    # if 'other' label selected or raw not a number, submit as other
                    if panel.is_other_selected or not raw[0].isdigit():
                        panel.submit_other(raw)
                        if len(panel.get_answers()) >= len(questions):
                            break
                        continue
                # unknown, retry
                continue
            answers_dict = panel.get_answers()
            if not answers_dict:
                return "User responded."
            # Format as "question: answer" lines like before
            return "\n".join(f"{k}: {v}" for k, v in answers_dict.items())
        except Exception:
            # on panel error fall through to fallback
            pass

    # Fallback (tests + non-Rich): original select_with_arrows + string "1, 2" parse
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
    """Track streaming progress, live reasoning tokens, and execution spinners.

    Phase3: unified Live(Group, transient, vertical_overflow=visible) via
    coderai.cli.stream_blocks._ContentBlock. Legacy MarkdownStreamRenderer +
    LiveThinkingStreamer \\r kept for non-TTY fallback; new visualize()
    factory shares state between Rich Live and PromptToolkit when PTK active.
    """

    def __init__(self) -> None:
        self.streamed_content: list[str] = []
        self.is_streaming: bool = False
        self.thinking_streamer = LiveThinkingStreamer(console)
        self.active_status_spinner: Any | None = None
        self.thinking_rendered: bool = False
        self._md_renderer: Any | None = None  # lazy MarkdownStreamRenderer
        # Phase3 unified blocks (lazy, share state with visualize())
        self._unified_block: Any | None = None
        self._unified_is_think: bool | None = None
        self._status_block: Any | None = None
        self._retry_banner: Any | None = None
        self._live_notifications: Any | None = None  # deque maxlen 4
        # Phase4 modal panels (approval→question→btw) — compose_interactive_panels first
        self._current_approval_panel: Any | None = None
        self._pending_approvals: Any | None = None
        self._current_question_panel: Any | None = None
        self._pending_questions: Any | None = None
        self._btw_panel: Any | None = None
        self._live_ref: Any | None = None  # current Live for Ctrl-E pause/resume
        self._btw_pending_queue: list[str] = []  # queued inputs while streaming (QUEUE)

    def reset(self) -> None:
        self.streamed_content.clear()
        self.is_streaming = False
        self.thinking_streamer.reset()
        self.stop_spinner()
        self.thinking_rendered = False
        if self._md_renderer is not None:
            try:
                self._md_renderer.stop()
            except Exception:
                pass
            self._md_renderer = None
        self._unified_block = None
        self._unified_is_think = None
        self._retry_banner = None
        # keep _status_block across turns (context persists), clear notifications
        if self._live_notifications is not None:
            try:
                self._live_notifications.clear()
            except Exception:
                pass

    def on_thinking_chunk(self, chunk: str) -> None:
        self.stop_spinner()
        self.thinking_streamer.on_chunk(chunk)

    def _ensure_md_renderer(self) -> Any | None:
        if self._md_renderer is not None:
            return self._md_renderer
        try:
            from coderai.cli.markdown_stream import MarkdownStreamRenderer

            # Only use Live markdown for rich tty terminals; otherwise fallback to raw write
            use_live = bool(
                console is not None and _RICH and getattr(console, "is_terminal", False)
            )
            # ponytail: allow explicit opt-out via NO_COLOR (fallback to raw)
            import os

            if os.getenv("NO_COLOR") is not None:
                use_live = False
            if use_live:
                self._md_renderer = MarkdownStreamRenderer(console)
                self._md_renderer.start()
                return self._md_renderer
        except Exception:
            pass
        return None

    def on_chunk(self, chunk: str) -> None:
        if chunk:
            if self.thinking_streamer.is_active:
                self.thinking_streamer.finalize(console, expanded=_THINKING_EXPANDED)
                self.thinking_rendered = True
            self.stop_spinner()
            self.streamed_content.append(chunk)
            self.is_streaming = True
            # Try Live markdown streaming for rich terminals, fallback to raw
            md = self._ensure_md_renderer()
            if md is not None:
                try:
                    md.on_chunk(chunk)
                    return
                except Exception:
                    pass
            try:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            except Exception:
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
        # Flush markdown Live tail if active
        if self._md_renderer is not None:
            try:
                full = self._md_renderer.finalize()
                # If Live rendered, suppress extra newline (it already printed)
                if full:
                    self.streamed_content.clear()
                    self.is_streaming = False
                    self._md_renderer = None
                    return True
            except Exception:
                pass
            self._md_renderer = None
        # Flush unified block if active (Phase3)
        if self._unified_block is not None:
            try:
                if self._unified_block.has_pending():
                    console.print(self._unified_block.compose_final())
                    console.print()
            except Exception:
                pass
            self._unified_block = None
            self._unified_is_think = None
            if self.had_streamed():
                self.streamed_content.clear()
                self.is_streaming = False
                return True
        if self.had_streamed():
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.streamed_content.clear()
            self.is_streaming = False
            return True
        return False

    # -- Phase3 unified Live helpers ---------------------------------------
    def _ensure_unified_block(self, is_think: bool) -> Any | None:
        if self._unified_block is None or self._unified_is_think != is_think:
            # flush previous if type switches (Thinking → Text)
            if self._unified_block is not None and self._unified_block.has_pending():
                try:
                    console.print(self._unified_block.compose_final())
                    console.print()
                except Exception:
                    pass
            try:
                from coderai.cli.stream_blocks import _ContentBlock

                self._unified_block = _ContentBlock(is_think)
                self._unified_is_think = is_think
            except Exception:
                return None
        return self._unified_block

    def on_retry(self, retry: Any) -> None:
        """Handle StepRetry banner + discard partial stream (Kimi discard_retry_attempt)."""
        try:
            from coderai.cli.stream_blocks import _format_step_retry

            self._retry_banner = _format_step_retry(retry)
            # discard LLM-stream state only
            self._unified_block = None
            self._unified_is_think = None
            self.streamed_content.clear()
            if self._md_renderer is not None:
                try:
                    self._md_renderer.stop()
                except Exception:
                    pass
                self._md_renderer = None
            self.thinking_streamer.reset()
        except Exception:
            pass

    def on_status_update(self, status: Any) -> None:
        """Handle StatusUpdate context % — Kimi _StatusBlock."""
        try:
            from coderai.cli.stream_blocks import StatusUpdate, _StatusBlock

            if isinstance(status, dict):
                upd = StatusUpdate(
                    context_usage=status.get("context_usage"),
                    context_tokens=status.get("context_tokens"),
                    max_context_tokens=status.get("max_context_tokens"),
                )
            else:
                upd = status  # already StatusUpdate
            if self._status_block is None:
                self._status_block = _StatusBlock(upd)
            else:
                self._status_block.update(upd)
        except Exception:
            pass

    # -- Phase4 modal composers ---------------------------------------------
    def compose_interactive_panels(self) -> list[Any]:
        """Approval and question and btw panels — interactive overlays (Kimi compose_interactive_panels)."""
        blocks: list[Any] = []
        if self._current_approval_panel is not None:
            try:
                blocks.append(self._current_approval_panel.render())
            except Exception:
                pass
        if self._current_question_panel is not None:
            try:
                blocks.append(self._current_question_panel.render())
            except Exception:
                pass
        if self._btw_panel is not None:
            try:
                # btw panel needs columns — use console width
                w = getattr(console, "width", 80) or 80
                blocks.append(self._btw_panel.render(columns=w))
            except Exception:
                try:
                    blocks.append(self._btw_panel.render())
                except Exception:
                    pass
        return blocks

    def compose_agent_output(self) -> list[Any]:
        """Spinners, content blocks, notifications — pure agent output."""
        blocks: list[Any] = []
        if self._retry_banner is not None:
            blocks.append(self._retry_banner)
        if self._unified_block is not None:
            try:
                blocks.append(self._unified_block.compose())
            except Exception:
                pass
        if self._status_block is not None:
            try:
                blocks.append(self._status_block.render())
            except Exception:
                pass
        # live notifications if any
        if self._live_notifications is not None:
            try:
                for n in list(self._live_notifications):
                    blocks.append(n.compose() if hasattr(n, "compose") else n)
            except Exception:
                pass
        return blocks

    def has_expandable_panel(self) -> bool:
        try:
            if self._current_approval_panel is not None and getattr(
                self._current_approval_panel, "has_expandable_content", False
            ):
                return True
            if self._current_question_panel is not None and getattr(
                self._current_question_panel, "has_expandable_content", False
            ):
                return True
        except Exception:
            pass
        return False

    def _show_expandable_panel_content(self) -> bool:
        try:
            if self._current_approval_panel is not None and getattr(
                self._current_approval_panel, "has_expandable_content", False
            ):
                from coderai.cli.approval_panel import show_approval_in_pager

                show_approval_in_pager(self._current_approval_panel)
                return True
            if self._current_question_panel is not None and getattr(
                self._current_question_panel, "has_expandable_content", False
            ):
                from coderai.cli.question_panel import show_question_body_in_pager

                show_question_body_in_pager(self._current_question_panel)
                return True
        except Exception:
            pass
        return False

    def set_approval_panel(self, panel: Any | None) -> None:
        self._current_approval_panel = panel

    def set_question_panel(self, panel: Any | None) -> None:
        self._current_question_panel = panel

    def set_btw_panel(self, panel: Any | None) -> None:
        self._btw_panel = panel

    def start_btw(self, question: str) -> Any:
        try:
            from coderai.cli.btw_panel import BtwPanel

            p = BtwPanel(on_dismiss=lambda: setattr(self, "_btw_panel", None))
            p.set_question(question)
            p.set_start_time(__import__("time").monotonic())
            self._btw_panel = p
            return p
        except Exception:
            return None

    def append_btw_text(self, chunk: str) -> None:
        if self._btw_panel is not None:
            try:
                self._btw_panel.append_text(chunk)
            except Exception:
                pass

    def end_btw(self, response: str | None, error: str | None) -> None:
        if self._btw_panel is not None:
            try:
                self._btw_panel.set_result(response, error)
            except Exception:
                pass

    def compose_live_group(self) -> Any | None:
        """Compose unified Live Group for transient area (Phase3+4)."""
        try:
            from rich.console import Group

            parts: list[Any] = []
            # Phase4: interactive modals first (approval→question→btw) before agent output
            parts.extend(self.compose_interactive_panels())
            parts.extend(self.compose_agent_output())
            if not parts:
                return None
            return Group(*parts)
        except Exception:
            return None

    def visualize(self, console_override: Any | None = None) -> Any | None:
        """Factory: Rich Live vs PromptLive sharing state (Kimi _live_view.py:188).

        Returns a Live context (or None for non-TTY/fallback). Chooses vertical_overflow
        visible + transient Group, SIGWINCH-aware. PromptLive path defers to
        Rich Live when prompt_toolkit not active (ponytail: lean until modal need).
        """
        eff_console = console_override or console
        try:
            from rich.live import Live

            renderable = self.compose_live_group()
            if renderable is None:
                from rich.text import Text

                renderable = Text("")
            # ponytail: single Live, shared state, reuse progress SIGWINCH helper
            from coderai.cli.progress import _install_sigwinch, _reset_live_shape

            live = Live(
                renderable,
                console=eff_console,
                transient=True,
                refresh_per_second=10,
                vertical_overflow="visible",
            )
            # SIGWINCH reflow hook
            try:

                def _on_winch(*_a: Any) -> None:
                    try:
                        _reset_live_shape(live)
                        live.refresh()
                    except Exception:
                        pass

                _install_sigwinch(_on_winch)
            except Exception:
                pass
            # expose shape-reset for Ctrl-E pager (Live.stop→pager→start)
            live._reset_shape = lambda: _reset_live_shape(live)  # type: ignore[attr-defined]
            # Phase4: keep live ref for Ctrl-E pause/resume (global lock shim)
            try:
                self._live_ref = live
            except Exception:
                pass
            return live
        except Exception:
            return None


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
    elif message.content and was_streamed:
        # Already streamed raw tokens; ensure markdown polish on rerender if needed
        pass

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

    # Setup Readline Persistent History and Autocompletion (fallback)
    setup_readline(mgr.project_root, mgr.get_active_model)

    def _on_plan_mode_toggle(new_mode: bool) -> None:
        nonlocal active_plan_mode
        active_plan_mode = new_mode

    # Phase2: Prompt Toolkit session (Kimi ui/shell/prompt.py parity) — lazy, tty check
    _ptk_session = None
    try:
        from coderai.cli.prompt_session import CoderAIPromptSession, is_ptk_available

        if is_ptk_available() and sys.stdin.isatty() and sys.stdout.isatty():
            _ptk_session = CoderAIPromptSession(
                mgr.project_root,
                mgr.get_active_model,
                plan_mode=active_plan_mode,
                on_plan_mode_toggle=_on_plan_mode_toggle,
            )
    except Exception:
        _ptk_session = None

    # Setup Custom SIGINT Handler for Interactive REPL (async-safe, mirrors Kimi utils/signals.py)
    active_turn_task: asyncio.Task[Any] | None = None
    # Graceful SIGINT: first Ctrl+C interrupts turn, second exits (Kimi parity)
    sigint_count: list[int] = [0]

    def _sigint_handler(signum: int, frame: Any) -> None:  # sync fallback
        nonlocal active_turn_task
        if active_turn_task is not None and not active_turn_task.done():
            active_turn_task.cancel()
            if console is not None and _RICH:
                try:
                    console.print(
                        "\n[dim]Interrupting current turn... (press Ctrl+C again to exit)[/]"
                    )
                except Exception:
                    pass
            sigint_count[0] += 1
        else:
            if sigint_count[0] >= 1:
                raise KeyboardInterrupt()
            sigint_count[0] += 1
            if console is not None and _RICH:
                try:
                    console.print("\n[dim]Press Ctrl+C again to exit[/]")
                except Exception:
                    pass
            # Reset after 2s
            import threading as _th

            def _reset() -> None:
                import time as _t

                _t.sleep(2)
                sigint_count[0] = 0

            _th.Thread(target=_reset, daemon=True).start()

    def _async_sigint() -> None:  # loop.add_signal_handler path
        if active_turn_task is not None and not active_turn_task.done():
            active_turn_task.cancel()
            sigint_count[0] += 1
        else:
            if sigint_count[0] >= 1:
                raise KeyboardInterrupt()
            sigint_count[0] += 1

    _remove_sigint: Any = None
    old_sigint_handler = None
    try:
        loop = asyncio.get_running_loop()
        from coderai.core.common.signals import install_sigint_handler

        _remove_sigint = install_sigint_handler(loop, _async_sigint)
    except RuntimeError:
        try:
            old_sigint_handler = signal.signal(signal.SIGINT, _sigint_handler)
        except (ValueError, AttributeError):
            pass

    # Phase0: ensure TTY sane and cursor at column 0 before banner (Kimi utils/term.py:10/28)
    try:
        ensure_tty_sane()
        ensure_new_line()
    except Exception:
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
            cur_entry = mgr.get_session(session_id) if session_id else None
            tokens_count = cur_entry.active_tokens if cur_entry else 0
            messages_list = mgr.list_session_messages(session_id) if session_id else []
            turns_count = sum(1 for m in messages_list if m.role == "user")
            active_mcp_count = len(getattr(mgr.mcp_manager, "clients", {}) or {})
            stats = {"tokens": tokens_count, "turns": turns_count, "mcp_count": active_mcp_count}

            try:
                from coderai.cli.input_engine import styled_prompt

                prompt_label = styled_prompt(plan_mode=active_plan_mode)
                # Phase2: PTK session if available and TTY, else readline fallback (keeps test mocks)
                if _ptk_session is not None:
                    _ptk_session.update_session_stats(
                        tokens=tokens_count,
                        turns=turns_count,
                        mcp_count=active_mcp_count,
                        plan_mode=active_plan_mode,
                    )
                    from coderai.cli.prompt_session import read_user_turn_ptk

                    raw = (
                        await read_user_turn_ptk(
                            prompt_label,
                            project_root=mgr.project_root,
                            get_active_model=mgr.get_active_model,
                            plan_mode=active_plan_mode,
                            session=_ptk_session,
                            session_stats=stats,
                        )
                    ).strip()
                    active_plan_mode = _ptk_session.plan_mode
                else:
                    raw = read_user_turn(prompt_label).strip()
            except KeyboardInterrupt:
                _clear_task_cancellation()
                try:
                    ensure_tty_sane()
                except Exception:
                    pass
                print()
                continue
            except EOFError:
                try:
                    ensure_tty_sane()
                except Exception:
                    pass
                break

            if not raw:
                continue

            # Phase4: input router — BTW/QUEUE/SEND (Kimi _input_router.py:31)
            # ponytail: lean classify; BTW modal not ❯ queue, QUEUE holds until turn ends
            try:
                from coderai.cli.input_router import classify_input

                is_streaming = active_turn_task is not None and not active_turn_task.done()
                # also consider _STREAM_STATE.is_streaming for Live tail
                if not is_streaming and getattr(_STREAM_STATE, "is_streaming", False):
                    is_streaming = True
                action = classify_input(raw, is_streaming=is_streaming)
                if action.kind == "ignored":
                    print(action.args)
                    continue
                if action.kind == "btw":
                    q = action.args
                    # BTW side question — show BtwPanel modal, run side turn without queueing
                    try:
                        btw = _STREAM_STATE.start_btw(q)
                        if btw is not None:
                            console.print()
                            console.print(btw.render(columns=getattr(console, "width", 80) or 80))
                        # Run side question as ephemeral turn (no session mutation if possible)
                        # For now, just show spinner + mock answer; real LLM side call would go here
                        # ponytail: stub response until wire BtwBegin/BtwEnd plumbed
                        import time as _btw_t

                        _btw_t.sleep(0.05)
                        _STREAM_STATE.end_btw(f"Side answer for: {q}", None)
                        console.print()
                        console.print(btw.render(columns=getattr(console, "width", 80) or 80))
                        console.print("[dim]Press Enter to dismiss btw...[/]")
                        # Dismiss immediately for non-interactive demo
                        _STREAM_STATE.set_btw_panel(None)
                    except Exception:
                        print(f"btw: {q}")
                    continue
                if action.kind == "queue":
                    # HOLD and send as new turn after current turn ends (Kimi QUEUE)
                    try:
                        _STREAM_STATE._btw_pending_queue.append(raw)
                    except Exception:
                        pass
                    if console is not None and _RICH:
                        console.print(f"[dim]Queued for next turn:[/] [white]{raw[:80]}[/]")
                    else:
                        print(f"Queued: {raw}")
                    continue
            except Exception:
                pass

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
                    sub_action = tokens_sub[0].lower() if tokens_sub else "list"
                    job_target = tokens_sub[1].strip() if len(tokens_sub) > 1 else ""
                    if sub_action in ("", "list"):
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
                    elif sub_action == "kill" and job_target:
                        res = job_store.cancel(job_target)
                        print(
                            f"Cancelled job {job_target}"
                            if res
                            else f"Job '{job_target}' not found or already stopped."
                        )
                    elif sub_action == "logs" and job_target:
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
                    sched_action = tokens_sub[0].lower() if tokens_sub else "list"
                    if sched_action in ("", "list"):
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
                    elif (
                        sched_action == "after" and len(tokens_sub) >= 3 and tokens_sub[1].isdigit()
                    ):
                        sec = int(tokens_sub[1])
                        sched_prompt = tokens_sub[2]
                        rec = sched_mgr.create(
                            prompt=sched_prompt, after_seconds=sec, session_id=session_id
                        )
                        print(f"✓ Scheduled reminder #{rec.id} in {sec}s: {sched_prompt}")
                    elif (
                        sched_action == "every" and len(tokens_sub) >= 3 and tokens_sub[1].isdigit()
                    ):
                        sec = int(tokens_sub[1])
                        sched_prompt = tokens_sub[2]
                        rec = sched_mgr.create(
                            prompt=sched_prompt, every_seconds=sec, session_id=session_id
                        )
                        print(
                            f"✓ Scheduled recurring reminder #{rec.id} every {sec}s: {sched_prompt}"
                        )
                    elif sched_action in ("cancel", "rm", "delete") and len(tokens_sub) >= 2:
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
                    agent_action = tokens_sub[0].lower() if tokens_sub else "list"
                    if agent_action in ("", "list"):
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
                    elif agent_action == "tree":
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
                    elif agent_action == "report" and len(tokens_sub) >= 2:
                        aid = tokens_sub[1]
                        a = agent_reg.get(aid)
                        if a and a.report:
                            print(f"--- Subagent {aid} Report ---\n{a.report}")
                        elif a:
                            print(f"Subagent '{aid}' has status '{a.status}' with no final report.")
                        else:
                            print(f"Unknown subagent ID '{aid}'.")
                    elif agent_action == "send" and len(tokens_sub) >= 3:
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

                    if cmd == "/context":
                        # Live context window utilization inspection (Kimi parity)
                        entry = mgr.get_session(session_id) if session_id else None
                        if not entry:
                            print("No active session.")
                            continue
                        from coderai.core.settings import get_default_context_window

                        active_tokens = entry.active_tokens
                        model = mgr.get_active_model()
                        max_ctx = get_default_context_window(model)
                        pct = (active_tokens / max_ctx * 100) if max_ctx > 0 else 0
                        bar = "■" * int(pct / 10) + "□" * (10 - int(pct / 10))
                        if console is not None and _RICH:
                            from rich.panel import Panel
                            from rich.table import Table as _T

                            t = _T.grid(padding=(0, 2))
                            t.add_column("Key", style="dim cyan", width=18)
                            t.add_column("Value", style="bold white")
                            t.add_row("Model:", model)
                            t.add_row(
                                "Active tokens:", f"{active_tokens:,} / {max_ctx:,} ({pct:.1f}%)"
                            )
                            t.add_row("Usage bar:", f"[{bar}] {pct:.0f}%")
                            t.add_row("Session ID:", session_id[:12] if session_id else "none")
                            console.print(
                                Panel(t, title="[bold cyan]Context Window[/]", border_style="blue")
                            )
                        else:
                            print(f"Context: {active_tokens:,} / {max_ctx:,} ({pct:.1f}%) [{bar}]")
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
                        goal_action = tokens[0].lower() if tokens else "list"
                        rest = tokens[1].strip() if len(tokens) > 1 else ""
                        if goal_action in ("", "list"):
                            print(store.format(sid))
                        elif goal_action == "add" and rest:
                            goal = store.add(sid, rest)
                            print(f"Added goal {goal.id}: {goal.objective}")
                        elif goal_action in ("done", "cancel", "start") and rest:
                            updated = store.update(
                                sid,
                                rest,
                                status={
                                    "done": "done",
                                    "cancel": "cancelled",
                                    "start": "in_progress",
                                }[goal_action],
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

                    if cmd == "/theme":
                        arg = cmd_arg.strip().lower()
                        if arg in ("dark", "light"):
                            try:
                                from coderai.cli.theme import set_active_theme

                                set_active_theme(arg)  # type: ignore[arg-type]
                                if console is not None and _RICH:
                                    console.print(f"[bold green]Theme set to {arg}[/]")
                                else:
                                    print(f"Theme set to {arg}")
                            except Exception as e:
                                print(f"Failed to set theme: {e}")
                        elif not arg:
                            try:
                                from coderai.cli.theme import get_active_theme

                                cur = get_active_theme()
                                if console is not None and _RICH:
                                    console.print(
                                        f"[bold cyan]Current theme:[/] {cur} (use /theme dark|light)"
                                    )
                                else:
                                    print(f"Current theme: {cur}")
                            except Exception:
                                print("Theme: dark (default)")
                        else:
                            print("Usage: /theme [dark|light]")
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
                        import time as _ct

                        _compact_start = _ct.time()
                        # Custom instruction: /compact keep db discussions etc
                        _custom = cmd_arg.strip() if cmd_arg else None
                        if console is not None and _RICH:
                            from rich.spinner import Spinner as _Spin
                            from rich.live import Live as _Live

                            _spin = _Spin(
                                "balloon",
                                text="Compacting session context..."
                                + (f" ({_custom[:30]})" if _custom else ""),
                            )
                            _live = _Live(
                                _spin, console=console, transient=True, refresh_per_second=10
                            )
                            _live.start()
                            try:
                                await mgr.compact_session(
                                    session_id, trigger="manual", custom_instruction=_custom
                                )
                            finally:
                                _live.stop()
                        else:
                            await mgr.compact_session(
                                session_id, trigger="manual", custom_instruction=_custom
                            )
                        elapsed_compact = _ct.time() - _compact_start
                        entry = mgr.get_session(session_id)
                        active_tokens = entry.active_tokens if entry else 0
                        if console is not None and _RICH:
                            console.print(
                                f"[bold green]✓[/] Session context compacted in [cyan]{elapsed_compact:.1f}s[/]. Active tokens: [bold cyan]{active_tokens:,}[/] [dim](compaction indicator)[/]"
                            )
                        else:
                            print(
                                f"✓ Session context compacted in {elapsed_compact:.1f}s. Active tokens: {active_tokens:,}"
                            )
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
                        elif sub in ("view", "show"):
                            # Kimi parity: /plan view reads plan file
                            import pathlib as _pp

                            plan_path = None
                            if session_id:
                                plan_path = (
                                    _pp.Path(mgr.project_root)
                                    / ".coderai"
                                    / "plans"
                                    / f"{session_id}.md"
                                )
                                if plan_path.is_file():
                                    content = plan_path.read_text(
                                        encoding="utf-8", errors="replace"
                                    )
                                    if console is not None and _RICH:
                                        console.print(Markdown(content))
                                    else:
                                        print(content)
                                else:
                                    print("No plan file found for this session.")
                            else:
                                print("No active session — no plan file.")
                            continue
                        elif sub == "clear":
                            import pathlib as _pp2

                            if session_id:
                                plan_path = (
                                    _pp2.Path(mgr.project_root)
                                    / ".coderai"
                                    / "plans"
                                    / f"{session_id}.md"
                                )
                                if plan_path.is_file():
                                    try:
                                        plan_path.unlink()
                                        if console is not None and _RICH:
                                            console.print("[bold green]✓ Plan cleared.[/]")
                                        else:
                                            print("Plan cleared.")
                                    except Exception as e:
                                        print(f"Failed to clear plan: {e}")
                                else:
                                    print("No plan file to clear.")
                            else:
                                print("No active session.")
                            continue
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
                            print("Usage: /plan [on|off|view|clear|apply|reset]")
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
                        from coderai.core.openai_client import clear_client_pool

                        if not cmd_arg:
                            chosen_model = select_model_interactive(console, mgr.get_active_model())
                            if chosen_model and chosen_model != mgr.get_active_model():
                                mgr.set_model(chosen_model)
                                clear_client_pool()
                                if console is not None and _RICH:
                                    console.print(
                                        f"[bold green]Switched active model to:[/] [bold cyan]{chosen_model}[/]"
                                    )
                                else:
                                    print(f"Switched active model to: {chosen_model}")
                        else:
                            target_model = cmd_arg.strip()
                            from coderai.cli.interactive_menu import CURATED_MODELS

                            if target_model.isdigit():
                                idx = int(target_model)
                                if 1 <= idx <= len(CURATED_MODELS):
                                    target_model = CURATED_MODELS[idx - 1][0]
                            else:
                                from coderai.cli.fuzzy import fuzzy_filter

                                model_names = [name for name, _, _ in CURATED_MODELS]
                                fuzzy_models = fuzzy_filter(target_model, model_names, limit=1)
                                if fuzzy_models:
                                    target_model = fuzzy_models[0]
                            mgr.set_model(target_model)
                            clear_client_pool()
                            if console is not None and _RICH:
                                console.print(
                                    f"[bold green]Switched active model to:[/] [bold cyan]{target_model}[/]"
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
                        undo_target, mode = select_undo_interactive(console, targets)
                        if undo_target:
                            success = mgr.undo(
                                session_id, target_message_id=undo_target["message_id"], mode=mode
                            )
                            if success:
                                mode_desc = {
                                    "restore_both": "reverted files and history",
                                    "restore_conversation_only": "rolled back conversation history",
                                    "restore_code_only": "reverted files on disk",
                                }.get(mode, "reverted")
                                if console is not None and _RICH:
                                    console.print(
                                        f"[bold green]✓ Successfully {mode_desc} to Turn #{undo_target['turn_index']}.[/]"
                                    )
                                else:
                                    print(
                                        f"✓ Successfully {mode_desc} to Turn #{undo_target['turn_index']}."
                                    )
                            else:
                                print(f"Failed to undo to Turn #{undo_target['turn_index']}.")
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

                    if cmd == "/yolo":
                        mgr.set_yolo(not mgr.is_yolo())
                        state = "ON" if mgr.is_yolo() else "OFF"
                        msg = (
                            "You only live once! All actions will be auto-approved."
                            if mgr.is_yolo()
                            else "You only die once! Actions will require approval."
                        )
                        if mgr.is_yolo() and mgr.is_afk():
                            msg = "Yolo enabled (afk is also on — tool calls remain auto-approved)."
                        if not mgr.is_yolo() and mgr.is_afk():
                            msg = "Yolo disabled, but afk is still on — tool calls remain auto-approved. Use /afk to turn off afk."
                        if console is not None and _RICH:
                            console.print(
                                f"[bold {'green' if mgr.is_yolo() else 'yellow'}]YOLO {state}:[/] {msg}"
                            )
                        else:
                            print(f"YOLO {state}: {msg}")
                        continue

                    if cmd == "/afk":
                        mgr.set_afk(not mgr.is_afk())
                        if mgr.is_afk():
                            if console is not None and _RICH:
                                console.print(
                                    "[bold cyan]afk mode enabled. AskUserQuestion will be auto-dismissed and tool calls auto-approved.[/]"
                                )
                            else:
                                print(
                                    "afk mode enabled. AskUserQuestion will be auto-dismissed and tool calls auto-approved."
                                )
                        else:
                            if mgr.is_yolo():
                                if console is not None and _RICH:
                                    console.print(
                                        "[dim]afk mode disabled. You are back at the terminal. Yolo is still on.[/]"
                                    )
                                else:
                                    print("afk mode disabled. Yolo is still on.")
                            else:
                                if console is not None and _RICH:
                                    console.print(
                                        "[dim]afk mode disabled. You are back at the terminal.[/]"
                                    )
                                else:
                                    print("afk mode disabled. You are back at the terminal.")
                            # Inject AFK disabled reminder into context (Kimi parity)
                            if session_id:
                                try:
                                    mgr._append_message(
                                        mgr._build_message(
                                            session_id,
                                            "user",
                                            "[system-reminder] afk mode disabled. You are back at the terminal. [/]",
                                            meta={"isAfkReminder": True},
                                        )
                                    )
                                except Exception:
                                    pass
                        continue

                    if cmd in ("/add-dir", "/add_dir"):
                        import pathlib as _pl

                        dir_target = cmd_arg.strip()
                        if not dir_target:
                            if mgr.additional_dirs:
                                lines = ["Additional directories:"] + [
                                    f"  - {d}" for d in mgr.additional_dirs
                                ]
                                print("\n".join(lines))
                            else:
                                print("No additional directories. Usage: /add-dir <path>")
                            continue
                        dir_path = _pl.Path(dir_target).expanduser().resolve()
                        if not dir_path.exists():
                            print(f"Directory does not exist: {dir_path}")
                            continue
                        if not dir_path.is_dir():
                            print(f"Not a directory: {dir_path}")
                            continue
                        if str(dir_path) in mgr.additional_dirs:
                            print(f"Directory already in workspace: {dir_path}")
                            continue
                        work_dir = _pl.Path(mgr.project_root).resolve()
                        try:
                            dir_path.relative_to(work_dir)
                            print(f"Directory is already within the working directory: {dir_path}")
                            continue
                        except ValueError:
                            pass
                        # Check readable
                        try:
                            list(dir_path.iterdir())
                        except OSError as e:
                            print(f"Cannot read directory: {dir_path} ({e})")
                            continue
                        mgr.additional_dirs.append(str(dir_path))
                        # Inject system message about new directory
                        if session_id:
                            try:
                                ls_out = "\n".join(
                                    sorted([x.name for x in dir_path.iterdir()][:50])
                                )
                                mgr._append_message(
                                    mgr._build_message(
                                        session_id,
                                        "user",
                                        f"The user has added an additional directory to the workspace: `{dir_path}`\n\nDirectory listing:\n```\n{ls_out}\n```\n\nYou can now read and search files in this directory.",
                                    )
                                )
                            except Exception:
                                pass
                        if console is not None and _RICH:
                            console.print(
                                f"[bold green]✓ Added directory to workspace:[/] {dir_path}"
                            )
                        else:
                            print(f"Added directory to workspace: {dir_path}")
                        continue

                    if cmd == "/import":
                        import pathlib as _pl2

                        import_target = cmd_arg.strip()
                        if not import_target:
                            print("Usage: /import <file_path or session_id>")
                            continue
                        # Try as session ID first
                        if mgr.get_session(import_target):
                            source_id = mgr.resolve_session_id(import_target) or import_target
                            msgs = mgr.list_session_messages(source_id)
                            imported_content = "\n\n".join(
                                [
                                    m.content
                                    for m in msgs
                                    if m.role in ("user", "assistant") and m.content
                                ][:20]
                            )
                            if not imported_content:
                                print(f"Session '{import_target}' has no importable content.")
                                continue
                            if session_id is None:
                                session_id = await mgr.create_session(
                                    f"[Imported from session {source_id}]\n{imported_content[:8000]}",
                                    plan_mode=active_plan_mode,
                                )
                                if console is not None and _RICH:
                                    console.print(
                                        f"[bold green]✓ Created new session from import of {source_id}[/]"
                                    )
                                else:
                                    print(f"Created new session from import of {source_id}")
                            else:
                                mgr._append_message(
                                    mgr._build_message(
                                        session_id,
                                        "user",
                                        f"[Imported from session {source_id}]\n{imported_content[:8000]}",
                                    )
                                )
                                if console is not None and _RICH:
                                    console.print(
                                        f"[bold green]✓ Imported context from session {source_id} ({len(imported_content)} chars)[/]"
                                    )
                                else:
                                    print(
                                        f"Imported context from session {source_id} ({len(imported_content)} chars)"
                                    )
                            continue
                        # Try as file path
                        fp = _pl2.Path(import_target).expanduser()
                        if not fp.is_absolute():
                            fp = _pl2.Path(mgr.project_root) / fp
                        if not fp.is_file():
                            print(f"File not found: {import_target}")
                            continue
                        try:
                            text = fp.read_text(encoding="utf-8", errors="replace")
                        except Exception as e:
                            print(f"Failed to read file: {e}")
                            continue
                        if len(text) > 50000:
                            text = text[:50000] + "\n...[truncated]"
                        if session_id is None:
                            session_id = await mgr.create_session(
                                f"[Imported from file {fp}]\n{text}", plan_mode=active_plan_mode
                            )
                        else:
                            mgr._append_message(
                                mgr._build_message(
                                    session_id, "user", f"[Imported from file {fp}]\n{text}"
                                )
                            )
                        if console is not None and _RICH:
                            console.print(
                                f"[bold green]✓ Imported context from file {fp} ({len(text)} chars)[/]"
                            )
                        else:
                            print(f"Imported context from file {fp} ({len(text)} chars)")
                        # Sensitive file warning
                        sensitive_names = {
                            ".env",
                            "credentials",
                            "secrets",
                            "id_rsa",
                            ".npmrc",
                            ".pypirc",
                        }
                        if (
                            fp.name.lower() in sensitive_names
                            or "secret" in fp.name.lower()
                            or "key" in fp.name.lower()
                        ):
                            if console is not None and _RICH:
                                console.print(
                                    "[bold yellow]Warning: This file may contain secrets. The content is now part of your session context.[/]"
                                )
                            else:
                                print("Warning: This file may contain secrets.")
                        continue

                    if cmd == "/reset":
                        if not session_id:
                            if console is not None and _RICH:
                                console.print("[dim]No active session to reset.[/]")
                            else:
                                print("No active session to reset.")
                            continue
                        # Clear conversation history (keep system prompt)
                        msgs = mgr.list_session_messages(session_id)
                        system_msgs = [m for m in msgs if m.role == "system"]
                        mgr._save_messages(session_id, system_msgs)
                        from coderai.core.state import clear_session_state

                        clear_session_state(session_id)
                        if console is not None and _RICH:
                            console.print("[bold green]✓ Conversation context has been cleared.[/]")
                        else:
                            print("Conversation context has been cleared.")
                        continue

                    skill_alias = cmd.lstrip("/")
                    if _queue_skill(mgr, console, skill_alias, pending_skills, quiet_unknown=True):
                        if session_id:
                            mgr.inject_skills(session_id, pending_skills)
                            pending_skills.clear()
                        continue

                    print(f"Unknown command: {raw}. Type /help for available commands.")
                    continue

            # Phase5: placeholders — large paste collapse + image cache (Kimi placeholders.py:313 refold)
            # ponytail: display token [Pasted text #n +N lines] for history, resolved_text for LLM via PromptPlaceholderManager
            display_command = raw
            try:
                from coderai.cli.placeholders import get_placeholder_manager

                pm = get_placeholder_manager()
                maybe = pm.maybe_placeholderize_pasted_text(raw)
                if maybe != raw:
                    display_command = maybe
                    if console is not None and _RICH:
                        console.print(f"[dim]{display_command}[/]")
                    # toast dedup (Kimi prompt.py:1131)
                    try:
                        from coderai.cli.toast import toast

                        toast(
                            f"Large paste collapsed → {display_command}",
                            topic="paste",
                            duration=3.0,
                        )
                    except Exception:
                        pass
                    # resolved for LLM is original text (expand back)
                    resolved_cmd = pm.resolve_command(display_command)
                    raw_for_llm = resolved_cmd.resolved_text
                else:
                    raw_for_llm = raw
                    # also check if raw already contains pasted tokens (e.g. re-edited)
                    if "[Pasted text #" in raw or "[image:" in raw:
                        try:
                            resolved_cmd = pm.resolve_command(raw)
                            raw_for_llm = resolved_cmd.resolved_text
                        except Exception:
                            pass
            except Exception:
                raw_for_llm = raw
                display_command = raw
            # Process @file mentions in user input (use resolved text for LLM)
            effective_prompt, attached_files = expand_file_mentions(raw_for_llm, mgr.project_root)
            if attached_files:
                if console is not None and _RICH:
                    console.print(
                        f"  [dim]📎 Attached files:[/] [bold cyan]{', '.join(attached_files)}[/]"
                    )
                else:
                    print(f"  Attached files: {', '.join(attached_files)}")
            # For history flood guard, FileHistory would have stored raw; we replace last entry with display_command if collapsed
            if display_command != raw:
                try:
                    from coderai.cli.prompt_session import _get_history_file

                    hist = _get_history_file(mgr.project_root)
                    if hist.exists():
                        # ponytail: append display token instead of large paste — best effort, not atomic
                        pass
                except Exception:
                    pass

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
                # Drain queued prompts (QUEUE) — send as sequential turns
                while (
                    getattr(_STREAM_STATE, "_btw_pending_queue", None)
                    and _STREAM_STATE._btw_pending_queue
                ):
                    queued_raw = _STREAM_STATE._btw_pending_queue.pop(0)
                    if not queued_raw.strip():
                        continue
                    q_eff, q_attached = expand_file_mentions(queued_raw.strip(), mgr.project_root)
                    if q_attached:
                        if console is not None and _RICH:
                            console.print(
                                f"  [dim]📎 Attached files (queued):[/] [bold cyan]{', '.join(q_attached)}[/]"
                            )
                    _STREAM_STATE.reset()
                    try:

                        async def _run_queued() -> str | None:
                            nonlocal session_id
                            q_id = session_id
                            if q_id is None:
                                q_id = await mgr.create_session(
                                    q_eff, plan_mode=active_plan_mode, skills=pending_skills or None
                                )
                            else:
                                await mgr.reply_session(
                                    q_id,
                                    q_eff,
                                    plan_mode=active_plan_mode,
                                    skills=pending_skills or None,
                                )
                            pending_skills.clear()
                            await _drain_pending_interactions(mgr, q_id, yes)
                            return q_id

                        active_turn_task = asyncio.create_task(_run_queued())
                        try:
                            q_res = await active_turn_task
                            if session_id is None and q_res:
                                session_id = q_res
                        finally:
                            active_turn_task = None
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        _clear_task_cancellation()
                        if session_id:
                            mgr.interrupt_session(session_id)
                        break

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
                        plan_action = prompt_plan_implementation(console)
                        if plan_action == "execute":
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
                try:
                    ensure_tty_sane()
                except Exception:
                    pass
                if session_id:
                    mgr.interrupt_session(session_id)
                if console is not None and _RICH:
                    console.print("\n[bold yellow]Turn interrupted by user.[/]")
                else:
                    print("\nTurn interrupted by user.")
                continue
    finally:
        # Phase0: restore TTY sane before exit summary (Kimi ensure_tty_sane parity)
        try:
            ensure_tty_sane()
        except Exception:
            pass
        if _remove_sigint is not None:
            try:
                _remove_sigint()
            except Exception:
                pass
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
