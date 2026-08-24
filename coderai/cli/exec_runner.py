"""Headless non-interactive execution runner.

Provides `--exec` batch execution mode for automated pipelines, CI/CD scripts,
and command-line one-shot invocations with structured exit codes.
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.markdown import Markdown

from coderai.cli.session_factory import build_session_manager, close_session_manager
from coderai.cli.thinking import render_thinking_block
from coderai.core.openai_client import create_openai_client as _core_client
from coderai.core.session import SessionMessage

console = Console()


def _print_text(text: str) -> None:
    console.print(text)


def _print_markdown(markdown_text: str) -> None:
    console.print(Markdown(markdown_text))


async def run_exec_session(
    prompt: str,
    *,
    project_root: str = ".",
    model: str | None = None,
    resume_session_id: str | None = None,
    plan_mode: bool = False,
    auto_approve: bool = False,
    verbose: bool = False,
    preset: str = "core",
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
        preset: Tool preset to use (default: core).

    Returns:
        0 on success, non-zero exit code on failure.
    """
    if not prompt or not prompt.strip():
        _print_text("[Error] No prompt provided to --exec.")
        return 1

    had_error = False

    def on_assistant_message(msg: SessionMessage, completed: bool) -> None:
        if msg.thinking and verbose:
            render_thinking_block(console, msg.thinking, expanded=True)
        if msg.content and completed:
            _print_markdown(msg.content)

    manager = build_session_manager(
        project_root,
        model=model,
        preset=preset,
        on_assistant_message=on_assistant_message,
        non_interactive=True,
        client_factory=_core_client,
    )

    try:
        await manager.init_mcp_servers()
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
                        {"toolCallId": r.get("toolCallId"), "permission": "allow"} for r in requests
                    ]
                    await manager.respond_permissions(session_id, replies)
                else:
                    # In non-interactive mode without auto_approve, deny and explain
                    _print_text(
                        "[coderai] Permission required for tool execution, but running non-interactively without --yes."
                    )
                    replies = [
                        {"toolCallId": r.get("toolCallId"), "permission": "deny"} for r in requests
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
        await close_session_manager(manager)
