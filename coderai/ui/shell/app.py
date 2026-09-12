"""Rich terminal interface over the CoderAI modular engine.

Presentation layer: argparse, interactive REPL, markdown rendering, tool execution cards,
diff previews, thinking mode summaries, dynamic status bar, interactive menus, permission
flows, autocompletion, session management, and slash commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys
from typing import Any

from coderai.ui.shell.slash import (
    SlashAction,
    ShellContext,
    dispatch_slash_command,
    parse_slash_command,
    render_help,
)
from coderai.ui.shell.prompt import (
    expand_file_mentions,
    read_user_turn,
    setup_readline,
)
from coderai.utils.rich.diff_render import render_diff_preview
from coderai.cli.exit_summary import render_exit_summary
from coderai.ui.shell.session_picker import (
    select_session_interactive,
    select_with_arrows,
)
from coderai.cli.session_factory import build_session_manager, close_session_manager
from coderai.ui.shell.visualize._blocks import LiveThinkingStreamer, render_thinking_block
from coderai.ui.shell.visualize._blocks import render_tool_card
from coderai.ui.shell import render_welcome_screen
from coderai.soul.approval import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    append_project_permission_allows,
)
from coderai.soul.session.manager import SessionManager, SessionMessage
from coderai.skill import list_skills, load_skill

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from coderai.utils.term import ensure_new_line, ensure_tty_sane

_RICH = True
# Phase0: neutral-themed console with MANPAGER-safe pager (Kimi parity: ui/shell/console.py:63)
console: Any
try:
    from coderai.ui.shell.console import console as _kimi_console  # type: ignore[assignment]

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


from coderai.ui.shell.startup import _build_parser  # noqa: E402


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
                from coderai.ui.shell.visualize._approval_panel import ApprovalRequestPanel, show_approval_in_pager

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
                                from coderai.ui.shell.visualize._blocks import _reset_live_shape

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
            from coderai.ui.shell.visualize._question_panel import QuestionRequestPanel, show_question_body_in_pager

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
    coderai.ui.shell.visualize._blocks._ContentBlock. Legacy MarkdownStreamRenderer +
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
        # Kimi ``--final-message-only`` parity: collect chunks silently, emit once.
        self.silent: bool = False

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
        if self.silent:
            return
        self.stop_spinner()
        self.thinking_streamer.on_chunk(chunk)

    def _ensure_md_renderer(self) -> Any | None:
        if self._md_renderer is not None:
            return self._md_renderer
        try:
            from coderai.ui.shell.visualize._blocks import MarkdownStreamRenderer

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
                if self.silent:
                    try:
                        self.thinking_streamer.reset()
                    except Exception:
                        pass
                else:
                    self.thinking_streamer.finalize(console, expanded=_THINKING_EXPANDED)
                self.thinking_rendered = True
            self.stop_spinner()
            self.streamed_content.append(chunk)
            self.is_streaming = True
            if self.silent:
                return
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
        if self.silent:
            return
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
                from coderai.ui.shell.visualize._blocks import _ContentBlock

                self._unified_block = _ContentBlock(is_think)
                self._unified_is_think = is_think
            except Exception:
                return None
        return self._unified_block

    def on_retry(self, retry: Any) -> None:
        """Handle StepRetry banner + discard partial stream (Kimi discard_retry_attempt)."""
        try:
            from coderai.ui.shell.visualize._blocks import _format_step_retry

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
            from coderai.ui.shell.visualize._blocks import StatusUpdate, _StatusBlock

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
                from coderai.ui.shell.visualize._approval_panel import show_approval_in_pager

                show_approval_in_pager(self._current_approval_panel)
                return True
            if self._current_question_panel is not None and getattr(
                self._current_question_panel, "has_expandable_content", False
            ):
                from coderai.ui.shell.visualize._question_panel import show_question_body_in_pager

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
            from coderai.ui.shell.visualize._btw_panel import BtwPanel

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
            from coderai.ui.shell.visualize._blocks import _install_sigwinch, _reset_live_shape

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
    if _STREAM_STATE.silent:
        return
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
        from coderai.ui.shell.prompt import CoderAIPromptSession, is_ptk_available

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
        from coderai.utils.signals import install_sigint_handler

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
        active_agent=mgr.get_active_agent_role() if hasattr(mgr, "get_active_agent_role") else "default",
    )

    # Check if active model has a configured API key
    active_m = mgr.get_active_model()
    resolved_settings = mgr.get_resolved_settings()
    from coderai.llm import resolve_model_provider_routing

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
            active_role = (
                mgr.get_active_agent_role()
                if hasattr(mgr, "get_active_agent_role")
                else "default"
            )
            stats = {
                "tokens": tokens_count,
                "turns": turns_count,
                "mcp_count": active_mcp_count,
                "agent_role": active_role,
            }

            try:
                from coderai.ui.shell.prompt import styled_prompt

                prompt_label = styled_prompt(plan_mode=active_plan_mode)
                # Phase2: PTK session if available and TTY, else readline fallback (keeps test mocks)
                if _ptk_session is not None:
                    _ptk_session.update_session_stats(
                        tokens=tokens_count,
                        turns=turns_count,
                        mcp_count=active_mcp_count,
                        plan_mode=active_plan_mode,
                        agent_role=active_role,
                    )
                    from coderai.ui.shell.prompt import read_user_turn_ptk

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
                    # Kimi Ctrl-X parity: shell mode executes directly.
                    if getattr(_ptk_session, "shell_mode", False) and raw and not raw.startswith("/"):
                        import subprocess as _sp

                        try:
                            res = _sp.run(
                                raw,
                                shell=True,
                                cwd=mgr.project_root,
                                capture_output=True,
                                text=True,
                                timeout=120,
                            )
                            out = (res.stdout or "") + (res.stderr or "")
                            if console is not None and _RICH:
                                console.print(f"[dim]{out.strip()[:4000] or '(exit ' + str(res.returncode) + ')'}[/]")
                            else:
                                print(out.strip() or f"(exit {res.returncode})")
                        except Exception as e:
                            print(f"shell error: {e}")
                        continue
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
                from coderai.ui.shell.visualize._input_router import classify_input

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
                    # BTW side question — BtwPanel modal + isolated LLM call (Kimi btw.py).
                    try:
                        from coderai.soul.btw import run_side_question

                        btw = _STREAM_STATE.start_btw(q)
                        if btw is not None:
                            console.print()
                            console.print(btw.render(columns=getattr(console, "width", 80) or 80))

                        async def _run_btw() -> None:
                            try:
                                answer = await run_side_question(
                                    mgr,
                                    session_id,
                                    q,
                                    on_chunk=_STREAM_STATE.append_btw_text,
                                )
                                _STREAM_STATE.end_btw(answer, None)
                            except Exception as e:
                                _STREAM_STATE.end_btw(None, str(e))

                        active_turn_task = asyncio.create_task(_run_btw())
                        try:
                            await active_turn_task
                        finally:
                            active_turn_task = None
                        console.print()
                        if btw is not None:
                            console.print(btw.render(columns=getattr(console, "width", 80) or 80))
                        console.print("[dim]Press Enter to dismiss btw...[/]")
                        try:
                            if sys.stdin.isatty():
                                input()
                        except (EOFError, KeyboardInterrupt):
                            _clear_task_cancellation()
                        _STREAM_STATE.set_btw_panel(None)
                    except Exception as e:
                        print(f"btw failed: {e}")
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
                ctx = ShellContext(
                    mgr=mgr,
                    session_id=session_id,
                    active_plan_mode=active_plan_mode,
                    console=console,
                    yes=yes,
                    pending_skills=pending_skills,
                    ptk_session=_ptk_session,
                    thinking_expanded=_THINKING_EXPANDED,
                )
                action = await dispatch_slash_command(
                    cmd, cmd_arg, ctx, drain_fn=_drain_pending_interactions
                )
                session_id = ctx.session_id
                active_plan_mode = ctx.active_plan_mode
                _THINKING_EXPANDED = ctx.thinking_expanded
                if action == SlashAction.EXIT:
                    break
                elif action == SlashAction.TURN:
                    if ctx.turn_prompt:
                        raw = ctx.turn_prompt
                    else:
                        continue
                else:
                    continue

            # Phase5: placeholders — large paste collapse + image cache (Kimi placeholders.py:313 refold)
            # ponytail: display token [Pasted text #n +N lines] for history, resolved_text for LLM via PromptPlaceholderManager
            display_command = raw
            try:
                from coderai.ui.shell.placeholders import get_placeholder_manager

                pm = get_placeholder_manager()
                maybe = pm.maybe_placeholderize_pasted_text(raw)
                if maybe != raw:
                    display_command = maybe
                    if console is not None and _RICH:
                        console.print(f"[dim]{display_command}[/]")
                    # toast dedup (Kimi prompt.py:1131)
                    try:
                        from coderai.ui.shell.prompt import toast

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
                    from coderai.ui.shell.prompt import _get_history_file

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
                        from coderai.ui.shell.visualize._approval_panel import prompt_plan_review

                        decision = prompt_plan_review(console, reply)
                        action_taken = decision.get("action", "reject")
                        if action_taken in ("approve", "option"):
                            active_plan_mode = False
                            opt = decision.get("option")
                            follow = (
                                f"Proceed with the implementation of the approved plan (selected Option {opt})."
                                if opt
                                else "Proceed with the implementation of the approved plan."
                            )
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
                                    follow,
                                    plan_mode=False,
                                )
                                await _drain_pending_interactions(mgr, session_id, yes)

                            active_turn_task = asyncio.create_task(_run_plan_execution())
                            try:
                                await active_turn_task
                            finally:
                                active_turn_task = None
                        elif action_taken == "revise":
                            refine_input = (decision.get("feedback") or "").strip()
                            if not refine_input:
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
                        elif action_taken == "reject-exit":
                            active_plan_mode = False
                            if console is not None and _RICH:
                                console.print("[yellow]Plan rejected; exited plan mode.[/]")
                            else:
                                print("Plan rejected; exited plan mode.")
                        # plain "reject" stays in plan mode; conversation continues.
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
        # Kimi parity: SessionEnd + Notification hooks fire on REPL exit.
        try:
            from coderai.hooks.runner import run_notification, run_session_end

            if session_id:
                run_session_end(session_id, mgr.project_root, "exit")
                try:
                    entry = mgr.get_session(session_id)
                    turns = getattr(entry, "turn_count", 0) if entry else 0
                except Exception:
                    turns = 0
                run_notification(
                    session_id,
                    mgr.project_root,
                    sink="session",
                    notification_type="session_end",
                    title="Session ended",
                    body=f"turns={turns}",
                )
        except Exception:
            pass
        render_exit_summary(console, mgr, session_id)

    return 0


def _emit_final_message_only(mgr: SessionManager, session_id: str) -> None:
    """Print only the final assistant text (Kimi ``--final-message-only`` parity).

    Stdout-only, no Rich markup: safe for pipes (``| head``, ``$(...)``).
    Falls back to the index ``assistantReply`` when the log has no text.
    """
    text = ""
    try:
        for message in reversed(mgr.list_session_messages(session_id)):
            if message.role == "assistant" and (message.content or "").strip():
                text = message.content.strip()
                break
    except Exception:
        text = ""
    if not text:
        try:
            entry = mgr.get_session(session_id)
            text = str((entry.assistant_reply if entry else "") or "").strip()
        except Exception:
            text = ""
    if text:
        print(text, flush=True)


async def _run_once(
    mgr: SessionManager,
    prompt: str,
    yes: bool,
    plan_mode: bool = False,
    *,
    final_message_only: bool = False,
    output_format: str | None = None,
) -> int:
    """Execute a single prompt non-interactively and exit."""
    effective_prompt, _ = expand_file_mentions(prompt, mgr.project_root)
    _STREAM_STATE.reset()
    if final_message_only:
        # Kimi parity: silence streaming cards/spinners; emit final text only.
        _STREAM_STATE.silent = True
    try:
        session_id = await mgr.create_session(effective_prompt, plan_mode=plan_mode)
        await _drain_pending_interactions(mgr, session_id, yes)
        if output_format == "stream-json":
            # Kimi parity: emit buffered wire events as JSON lines on stdout.
            try:
                from coderai.wire.emitter import get_emitter

                for envelope in await get_emitter().drain_to_stream_json():
                    print(json.dumps(envelope, ensure_ascii=False), flush=True)
            except Exception:
                pass
        if final_message_only:
            _emit_final_message_only(mgr, session_id)
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
    finally:
        _STREAM_STATE.silent = False


def main(argv: list[str] | None = None) -> int:
    """Console entry point for CoderAI CLI."""
    from coderai.utils.proxy import normalize_proxy_env
    from coderai.log import enable_logging

    from coderai.utils.proctitle import init_process_name

    init_process_name("CoderAI")
    normalize_proxy_env()
    # --debug enables file logging; resolved pre-parse so startup crashes land
    # in ~/.coderai/logs/coderai.log (Kimi: enable_logging(debug)).
    _debug_early = "--debug" in (argv if argv is not None else sys.argv[1:])
    enable_logging(debug=_debug_early, redirect_stderr=False)
    # Kimi parity: real subcommands bypass the interactive parser entirely
    # (``kimi info|export|mcp`` stay usable with zero config / offline).
    # Only treat the first token as a subcommand when it is NOT consumed by
    # an option (e.g. ``-p info`` is a prompt, not the info subcommand).
    _raw = list(argv if argv is not None else sys.argv[1:])
    _first = _raw[0] if _raw else ""
    if _first in ("info", "export", "mcp", "plugin", "login", "logout", "acp"):
        if _first == "info":
            from coderai.cli.info import run_info

            return run_info(_raw[1:])
        if _first == "export":
            from coderai.cli.export import run_export

            return run_export(_raw[1:], project_root=str(pathlib.Path.cwd().resolve()))
        if _first == "mcp":
            from coderai.cli.mcp import run_mcp

            return run_mcp(_raw[1:])
        if _first == "plugin":
            from coderai.cli.plugin import run_plugin

            return run_plugin(_raw[1:])
        if _first == "login":
            from coderai.ui.shell.oauth import run_login

            return run_login(_raw[1:])
        if _first == "logout":
            from coderai.ui.shell.oauth import run_logout

            return run_logout(_raw[1:])
        if _first == "acp":
            # Kimi parity: run the ACP server on stdio (Agent Control Protocol).
            from coderai.acp import acp_main

            acp_main()
            return 0
    args = _build_parser().parse_args(argv)
    project_root = str(pathlib.Path.cwd().resolve())

    # Forward orchestration flags to the CODERAI_* environment contract so the
    # shared orchestration layer (and child processes) resolve one source.
    for flag, env_name in (
        ("max_subagent_depth", "CODERAI_MAX_SUBAGENT_DEPTH"),
        ("subagent_timeout", "CODERAI_SUBAGENT_TIMEOUT_SECONDS"),
        ("max_continuable_agents", "CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION"),
        ("max_running_jobs", "CODERAI_MAX_RUNNING_JOBS_PER_SESSION"),
        ("max_steps_per_turn", "CODERAI_MAX_STEPS_PER_TURN"),
        ("max_retries_per_step", "CODERAI_MAX_RETRIES_PER_STEP"),
        ("max_ralph_iterations", "CODERAI_MAX_RALPH_ITERATIONS"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            os.environ[env_name] = str(value)
    # Kimi parity: --work-dir switches the project root; --config-file/--config
    # redirect settings resolution; --skills-dir/--add-dir/--mcp-config-file preload.
    if getattr(args, "work_dir", None):
        project_root = str(pathlib.Path(args.work_dir).expanduser().resolve())
    if getattr(args, "config_file", None):
        os.environ["CODERAI_CONFIG_FILE"] = str(args.config_file)
    if getattr(args, "config_string", None):
        os.environ["CODERAI_CONFIG_STRING"] = str(args.config_string)
    if getattr(args, "skills_dirs", None):
        os.environ["CODERAI_SKILLS_DIRS"] = os.pathsep.join(args.skills_dirs)
    if getattr(args, "mcp_config_files", None):
        os.environ["CODERAI_MCP_CONFIG_FILES"] = os.pathsep.join(args.mcp_config_files)
    if getattr(args, "mcp_config_jsons", None):
        from coderai.mcp.files import collect_cli_mcp_overlays

        _cli_servers, _cli_warnings = collect_cli_mcp_overlays(
            config_jsons=list(args.mcp_config_jsons),
        )
        for _warning in _cli_warnings:
            print(f"Warning: {_warning}", file=sys.stderr)
        if _cli_servers:
            import json as _json

            os.environ["CODERAI_MCP_CONFIG_JSON"] = _json.dumps({"mcpServers": _cli_servers})
    if getattr(args, "agent", None):
        os.environ["CODERAI_AGENT"] = str(args.agent)
    if getattr(args, "agent_file", None):
        os.environ["CODERAI_AGENT_FILE"] = str(args.agent_file)

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
        from coderai.ui.shell.setup import run_setup_cli

        return run_setup_cli(args, project_root=project_root)

    if has_positional and has_prompt_flag:
        print(
            "Cannot use both a positional prompt and the --prompt (-p) flag together",
            file=sys.stderr,
        )
        return 1

    # Kimi parity: --quiet == --print --output-format text --final-message-only.
    # --print implies afk auto-approval for the invocation (no extra flag needed).
    if getattr(args, "quiet", False):
        args.print_mode = True
        args.output_format = args.output_format or "text"
        args.final_message_only = True
    if getattr(args, "final_message_only", False) and not getattr(args, "print_mode", False):
        print("--final-message-only requires --print.", file=sys.stderr)
        return 1
    if getattr(args, "input_format", None) and not getattr(args, "print_mode", False):
        print("--input-format requires --print.", file=sys.stderr)
        return 1
    if getattr(args, "output_format", None) and not getattr(args, "print_mode", False):
        print("--output-format requires --print.", file=sys.stderr)
        return 1
    if getattr(args, "wire", False) and getattr(args, "print_mode", False):
        print("Cannot use --wire together with --print.", file=sys.stderr)
        return 1
    if getattr(args, "continue_session", False) and args.resume is not None:
        print("Cannot use --continue together with --resume.", file=sys.stderr)
        return 1
    if getattr(args, "continue_session", False) and args.fork is not None:
        print("Cannot use --continue together with --fork.", file=sys.stderr)
        return 1
    if getattr(args, "continue_session", False) and args.last:
        print("Cannot use --continue together with --last.", file=sys.stderr)
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
        # Kimi parity: --print/--quiet non-interactive single-shot path.
        effective_yes = bool(args.yes or getattr(args, "print_mode", False))
        if getattr(args, "afk", False):
            os.environ["CODERAI_START_AFK"] = "1"
        if getattr(args, "thinking", None) is not None:
            os.environ["CODERAI_THINKING"] = "1" if args.thinking else "0"
        if has_exec and prompt_value:
            from coderai.ui.print import run_exec_session

            resume_id = args.resume if isinstance(args.resume, str) else None
            return await run_exec_session(
                prompt_value,
                project_root=project_root,
                model=args.model,
                resume_session_id=resume_id,
                plan_mode=args.plan,
                auto_approve=effective_yes,
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
        # Kimi parity: refresh OAuth-backed provider tokens at startup.
        try:
            from coderai.llm import ensure_oauth_fresh

            await ensure_oauth_fresh()
        except Exception:
            pass
        # Kimi parity: --add-dir preload + --continue resolution.
        if getattr(args, "add_dirs", None):
            import pathlib as _plm

            for _d in args.add_dirs:
                _p = _plm.Path(_d).expanduser().resolve()
                if _p.is_dir() and str(_p) not in mgr.additional_dirs:
                    mgr.additional_dirs.append(str(_p))
        resume_arg = args.resume
        last_arg = bool(args.last or getattr(args, "continue_session", False))
        if getattr(args, "afk", False):
            try:
                mgr.set_afk(True)
            except Exception:
                pass
        if getattr(args, "wire", False):
            # Kimi parity: --wire serves the wire protocol on stdio with a
            # quiet manager: interactive stream callbacks print Rich markup,
            # which would corrupt the JSON-RPC stream on stdout.
            from coderai.wire.server import run_wire_stdio

            wire_mgr = build_session_manager(
                project_root,
                model=args.model,
                preset=preset_mode,
                non_interactive=True,
            )
            if getattr(args, "add_dirs", None):
                for _d in args.add_dirs:
                    _p = _plm.Path(_d).expanduser().resolve()
                    if _p.is_dir() and str(_p) not in wire_mgr.additional_dirs:
                        wire_mgr.additional_dirs.append(str(_p))
            wire_session: str | None = None
            if isinstance(resume_arg, str) and resume_arg.strip():
                wire_session = wire_mgr.resolve_session_id(resume_arg.strip()) or resume_arg.strip()
            elif last_arg:
                _sessions = wire_mgr.list_sessions()
                wire_session = _sessions[0].id if _sessions else None
            try:
                return await run_wire_stdio(wire_mgr, wire_session)
            finally:
                await close_session_manager(wire_mgr)
        # Kimi parity: headless modes connect MCP inline; the interactive
        # shell defers to a background task (fast start, joined per turn).
        _will_print = bool(getattr(args, "print_mode", False) and prompt_value)
        _will_run_once = bool(prompt_value and not (resume_arg or args.fork or last_arg))
        if _will_print or _will_run_once:
            await mgr.init_mcp_servers()
        else:
            mgr.start_background_mcp_loading()
        try:
            if getattr(args, "print_mode", False) and prompt_value:
                # Read stdin when --input-format stream-json / piped input.
                stdin_prompt = ""
                if not sys.stdin.isatty() and (
                    getattr(args, "input_format", None) == "stream-json"
                    or (not has_prompt_flag and not has_positional)
                ):
                    try:
                        stdin_prompt = sys.stdin.read().strip()
                    except Exception:
                        stdin_prompt = ""
                full_prompt = prompt_value
                if getattr(args, "input_format", None) == "stream-json" and stdin_prompt:
                    try:
                        payload = json.loads(stdin_prompt)
                        if isinstance(payload, dict) and payload.get("prompt"):
                            full_prompt = str(payload["prompt"])
                    except Exception:
                        full_prompt = stdin_prompt or prompt_value
                elif stdin_prompt and not has_prompt_flag and not has_positional:
                    full_prompt = stdin_prompt
                # Kimi parity: --continue with a prompt resumes the latest
                # session instead of forking a fresh one.
                _resume_target: str | None = None
                if last_arg and not (resume_arg or args.fork):
                    from coderai.metadata import get_last_session_id

                    _sessions = mgr.list_sessions()
                    _resume_target = _sessions[0].id if _sessions else None
                    if _resume_target is None:
                        try:
                            _resume_target = get_last_session_id(project_root)
                        except Exception:
                            _resume_target = None
                    if _resume_target is not None:
                        await mgr.reply_session(
                            _resume_target, full_prompt, plan_mode=args.plan
                        )
                        await _drain_pending_interactions(mgr, _resume_target, effective_yes)
                        if getattr(args, "final_message_only", False):
                            _emit_final_message_only(mgr, _resume_target)
                        _entry = mgr.get_session(_resume_target)
                        return 1 if (_entry and _entry.status == "failed") else 0
                rc = await _run_once(
                    mgr,
                    full_prompt,
                    effective_yes,
                    plan_mode=args.plan,
                    final_message_only=bool(getattr(args, "final_message_only", False)),
                    output_format=getattr(args, "output_format", None),
                )
                if getattr(args, "output_format", None) in ("stream-json", "json"):
                    print(json.dumps({"exit_code": rc, "session": "print-mode"}))
                return rc
            if prompt_value and not (resume_arg or args.fork or last_arg):
                return await _run_once(
                    mgr,
                    prompt_value,
                    effective_yes,
                    plan_mode=args.plan,
                    final_message_only=bool(getattr(args, "final_message_only", False)),
                    output_format=getattr(args, "output_format", None),
                )
            return await _run_interactive(
                mgr,
                effective_yes,
                resume=resume_arg,
                fork=args.fork,
                last=last_arg,
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
