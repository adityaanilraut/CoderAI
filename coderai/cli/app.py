"""CLI — rich UI over coderai.core (mirrors DeepCode packages/cli).

Presentation layer: argparse, interactive REPL, markdown rendering, tool execution cards,
diff previews, thinking mode summaries, dynamic status bar, interactive menus, permission
flows, and slash commands. All engine logic resides in coderai.core.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import subprocess
import sys
from typing import Any

from coderai._version import __version__
from coderai.cli.diff_render import render_diff_preview
from coderai.cli.exit_summary import render_exit_summary
from coderai.cli.file_mention import expand_file_mentions
from coderai.cli.interactive_menu import (
    render_skills_interactive,
    select_model_interactive,
    select_session_interactive,
)
from coderai.cli.status_bar import render_status_bar
from coderai.cli.thinking import render_thinking_block
from coderai.cli.tool_card import render_tool_card
from coderai.cli.welcome import render_welcome_screen
from coderai.core.openai_client import create_openai_client as _core_client
from coderai.core.permissions import append_project_permission_allows
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
        prog="coderai", description="coderai — AI pair programming in your terminal."
    )
    parser.add_argument("prompt", nargs="*", help="initial prompt (non-interactive when provided)")
    parser.add_argument("--model", "-m", help="LLM model to use")
    parser.add_argument("--message", dest="message", help="send a single message and exit")
    parser.add_argument("--resume", help="resume an existing session by id")
    parser.add_argument("--plan", action="store_true", help="start session in Plan Mode")
    parser.add_argument("--yes", "-y", action="store_true", help="auto-approve all permissions")
    parser.add_argument("--verbose", "-v", action="store_true", help="print debug information")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _prompt_permissions(
    requests: list[dict[str, Any]], yes: bool
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

        if yes:
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
            body += f"\n[dim]Scopes:[/] {scopes_str}"

            panel = Panel(
                body,
                title=f"[bold yellow]Permission Required ({idx}/{len(requests)})[/]",
                border_style="yellow",
            )
            console.print(panel)
        else:
            print(f"\n--- Permission Required ({idx}/{len(requests)}) ---")
            print(f"[{name}] {command}")
            if description:
                print(f"  Description: {description}")
            if scopes:
                print(f"  Scopes: {', '.join(scopes)}")

        options: list[tuple[str, str, str]] = [("allow", "1", "Yes")]
        always_target = next((s for s in scopes if s in ALWAYS_ALLOWED_SCOPES), None)
        if always_target:
            options.append(
                ("always", "2", f"Yes, and always allow {describe_scope(always_target)}")
            )
        options.append(("deny", "3" if always_target else "2", "No"))

        print("  Options:")
        for _, key, label in options:
            print(f"    {key}. {label}")

        prompt_str = "  Allow? [1/2/3] (or y/n/a): "
        try:
            raw_choice = input(prompt_str).strip().lower()
        except (EOFError, KeyboardInterrupt):
            raw_choice = "n"

        if raw_choice in ("2", "a", "always") and always_target:
            replies.append({"toolCallId": tool_call_id, "permission": "allow"})
            always_allows.append(always_target)
        elif raw_choice in ("3", "n", "no", "deny") or (raw_choice == "2" and not always_target):
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


_STREAM_STATE = _StreamState()


def _on_assistant_message(message: SessionMessage, should_connect: bool) -> None:
    """Format and render assistant messages, thinking blocks, and tool executions."""
    meta = message.meta or {}
    if meta.get("asThinking"):
        render_thinking_block(console, message.content)
        return

    if message.role == "tool":
        render_tool_card(console, message)
        return

    if message.thinking:
        render_thinking_block(console, message.thinking)

    if message.content:
        # If content was already live-streamed, just append final newline
        if _STREAM_STATE.had_streamed():
            sys.stdout.write("\n")
            sys.stdout.flush()
            _STREAM_STATE.reset()
        else:
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
            replies, always = _prompt_permissions(entry.ask_permissions or [], yes)
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
    """Display interactive command help table."""
    if console is not None and _RICH and Table is not None:
        table = Table(title="CoderAI Interactive Slash Commands", border_style="cyan")
        table.add_column("Command", style="bold cyan", width=16)
        table.add_column("Description", style="white")
        table.add_row("/continue", "Continue bounded multi-step agent execution")
        table.add_row("/plan", "Toggle Plan Mode on/off with visual indicator")
        table.add_row("/undo", "Revert files and turn to previous checkpoint via GitFileHistory")
        table.add_row("/diff", "Show unified diff of changes made since session start")
        table.add_row("/model [name]", "Interactive model selector or switch to named model")
        table.add_row("/sessions", "Interactive sessions menu with quick-resume")
        table.add_row("/resume <id>", "Resume an existing session by ID directly")
        table.add_row("/new", "Start a fresh session")
        table.add_row("/skills", "Explore active and discovered skills")
        table.add_row("/help", "Show this command help menu")
        table.add_row("/exit, /quit", "Exit session with summary card")
        console.print(table)
    else:
        print("\nCommands:")
        print("  /continue      Continue bounded multi-step agent execution")
        print("  /plan          Toggle Plan Mode on/off")
        print("  /undo          Revert files and turn to previous checkpoint")
        print("  /diff          Show diff of changes made since session start")
        print("  /model [name]  Interactive model selector or switch model")
        print("  /sessions      Interactive sessions list & quick resume")
        print("  /resume <id>   Resume an existing session by ID")
        print("  /new           Start a fresh session")
        print("  /skills        List active and workspace skills")
        print("  /help          Show this help menu")
        print("  /exit, /quit   Quit CoderAI\n")


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


async def _run_interactive(
    mgr: SessionManager, yes: bool, resume: str | None, plan_mode: bool = False
) -> int:
    """Interactive REPL with rich welcome screen, dynamic status bar, and command selectors."""
    session_id = resume
    active_plan_mode = plan_mode
    if session_id and mgr.get_session(session_id) is None:
        print(f"No saved session with id '{session_id}'.")
        return 1

    # Render Welcome Screen & Brand Identity
    mcp_count = len(getattr(mgr.mcp_manager, "clients", {}) or {})
    render_welcome_screen(
        console,
        mgr.project_root,
        mgr.get_active_model(),
        plan_mode=active_plan_mode,
        mcp_servers_count=mcp_count,
    )

    try:
        while True:
            # Render Dynamic Status Bar
            cur_entry = mgr.get_session(session_id) if session_id else None
            tokens_count = cur_entry.active_tokens if cur_entry else 0
            render_status_bar(
                console,
                mgr.get_active_model(),
                tokens_count,
                active_plan_mode,
                mgr.project_root,
            )

            try:
                prompt_label = "coderai [plan]> " if active_plan_mode else "coderai> "
                raw = input(prompt_label).strip()
            except (EOFError, KeyboardInterrupt):
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

                if cmd == "/continue":
                    if not session_id:
                        print("No active session to continue.")
                        continue
                    _STREAM_STATE.reset()
                    await mgr.reply_session(session_id, "/continue")
                    await _drain_pending_interactions(mgr, session_id, yes)
                    continue

                if cmd == "/plan":
                    active_plan_mode = not active_plan_mode
                    status = "enabled" if active_plan_mode else "disabled"
                    if console is not None and _RICH:
                        console.print(f"[bold yellow]Plan Mode {status}.[/]")
                    else:
                        print(f"Plan Mode {status}.")
                    if session_id:
                        await mgr.reply_session(session_id, plan_mode=active_plan_mode)
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
                    chosen_session = select_session_interactive(console, sessions)
                    if chosen_session:
                        session_id = chosen_session
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

                if cmd == "/undo":
                    if session_id and mgr.undo(session_id):
                        if console is not None and _RICH:
                            console.print(
                                "[bold green]✓ Reverted files and history to previous checkpoint.[/]"
                            )
                        else:
                            print("Reverted files and history to previous checkpoint.")
                    else:
                        print("Nothing to undo in the active session.")
                    continue

                if cmd == "/new":
                    session_id = None
                    if console is not None and _RICH:
                        console.print("[bold cyan]Started a fresh session.[/]")
                    else:
                        print("Started a fresh session.")
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
            if session_id is None:
                session_id = await mgr.create_session(effective_prompt, plan_mode=active_plan_mode)
            else:
                await mgr.reply_session(session_id, effective_prompt, plan_mode=active_plan_mode)

            await _drain_pending_interactions(mgr, session_id, yes)
    finally:
        render_exit_summary(console, mgr, session_id)

    return 0


async def _run_once(mgr: SessionManager, prompt: str, yes: bool, plan_mode: bool = False) -> int:
    """Execute a single prompt non-interactively and exit."""
    effective_prompt, _ = expand_file_mentions(prompt, mgr.project_root)
    _STREAM_STATE.reset()
    session_id = await mgr.create_session(effective_prompt, plan_mode=plan_mode)
    await _drain_pending_interactions(mgr, session_id, yes)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console entry point for CoderAI CLI."""
    args = _build_parser().parse_args(argv)
    project_root = str(pathlib.Path.cwd().resolve())

    mgr = _build_manager(project_root, args.model)

    async def _main() -> int:
        await mgr.init_mcp_servers()
        try:
            if args.message:
                return await _run_once(mgr, args.message, args.yes, plan_mode=args.plan)
            if args.prompt:
                prompt = " ".join(args.prompt)
                return await _run_once(mgr, prompt, args.yes, plan_mode=args.plan)
            return await _run_interactive(mgr, args.yes, args.resume, plan_mode=args.plan)
        finally:
            mgr.dispose()

    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
