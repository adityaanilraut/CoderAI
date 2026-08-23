"""Headless non-interactive execution runner — port of deepcode exec-runner.ts.

Provides `--exec` batch execution mode for automated pipelines, CI/CD scripts,
and command-line one-shot invocations with structured exit codes.
"""

from __future__ import annotations

import asyncio
import sys

from coderai.cli.thinking import render_thinking_block
from coderai.core.openai_client import create_openai_client as _core_client
from coderai.core.session import SessionManager, SessionMessage

try:
    from rich.console import Console
    from rich.markdown import Markdown

    _RICH = True
    console: Console | None = Console()
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment,misc]
    Markdown = None  # type: ignore[assignment,misc]
    _RICH = False
    console = None


def _print_text(text: str) -> None:
    if console is not None and _RICH:
        console.print(text)
    else:
        print(text)


def _print_markdown(markdown_text: str) -> None:
    if console is not None and _RICH and Markdown is not None:
        console.print(Markdown(markdown_text))
    else:
        print(markdown_text)


async def run_exec_session(
    prompt: str,
    *,
    project_root: str = ".",
    model: str | None = None,
    resume_session_id: str | None = None,
    plan_mode: bool = False,
    auto_approve: bool = False,
    verbose: bool = False,
    preset: str = "benchmark",
) -> int:
    """Execute a prompt headless in `--exec` mode.

    Args:
        prompt: The user instruction to execute.
        project_root: Root workspace directory.
        model: Optional model override.
        resume_session_id: Session ID to resume if provided.
        plan_mode: Start in Plan Mode.
        auto_approve: Auto-approve all permission prompts.
        verbose: Verbose output.
        preset: Tool preset to use (default: benchmark).

    Returns:
        0 on success, non-zero exit code on failure.
    """
    if not prompt or not prompt.strip():
        _print_text("[Error] No prompt provided to --exec.")
        return 1

    last_assistant_text = ""
    had_error = False

    def on_assistant_message(msg: SessionMessage, completed: bool) -> None:
        nonlocal last_assistant_text
        if msg.thinking and verbose:
            render_thinking_block(console, msg.thinking, expanded=True)
        if msg.content:
            last_assistant_text = msg.content
            if completed:
                _print_markdown(msg.content)

    def on_stream_chunk(chunk: str) -> None:
        if not _RICH:
            sys.stdout.write(chunk)
            sys.stdout.flush()

    manager = SessionManager(
        project_root=project_root,
        create_openai_client=_core_client,
        get_resolved_settings=lambda: {
            "preset": preset,
            "toolsPreset": preset,
            "autoApprove": auto_approve,
        },
        render_markdown=lambda t: t,
        on_assistant_message=on_assistant_message,
        on_stream_chunk=on_stream_chunk if not _RICH else None,
        non_interactive=True,
    )

    if model:
        manager.set_model(model)

    try:
        # Determine session ID
        if resume_session_id:
            session_id = resume_session_id
            existing = manager.get_session(session_id)
            if not existing:
                _print_text(f"[Error] Session not found: {resume_session_id}")
                return 1
            await manager.reply_session(session_id, prompt)
        else:
            session_id = await manager.create_session(prompt, plan_mode=plan_mode)

        # Handle any permission requests if they occurred during turn
        while True:
            entry = manager.get_session(session_id)
            if not entry:
                break

            if entry.status == "ask_permission":
                requests = entry.ask_permissions or []
                if auto_approve:
                    # Grant all requested permissions
                    replies = [
                        {"toolCallId": r.get("toolCallId"), "decision": "allow"} for r in requests
                    ]
                    await manager.respond_permissions(session_id, replies)
                else:
                    # In non-interactive mode without auto_approve, deny and explain
                    _print_text(
                        "[coderai] Permission required for tool execution, but running non-interactively without --yes."
                    )
                    replies = [
                        {"toolCallId": r.get("toolCallId"), "decision": "deny"} for r in requests
                    ]
                    await manager.respond_permissions(session_id, replies)
                continue

            if entry.status in (
                "completed",
                "failed",
                "interrupted",
                "permission_denied",
                "waiting_for_user",
            ):
                if entry.status == "failed":
                    had_error = True
                    _print_text(f"[Error] Session failed: {entry.fail_reason or 'Unknown error'}")
                break

            await asyncio.sleep(0.05)

        return 1 if had_error else 0

    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    except Exception as e:
        _print_text(f"[Error] Execution error: {e}")
        return 1
    finally:
        try:
            manager.dispose()
        except Exception:
            pass
