"""Print mode — non-interactive, single-shot UI (`coderai/ui/print/` parity).

Drives one prompt to completion headlessly for automated pipelines, CI/CD
scripts, and command-line one-shot invocations, with structured exit codes.

Companion module: :mod:`coderai.ui.print.visualize` renders the wire event
stream for ``--output-format json`` / ``final-only`` print modes.
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.markdown import Markdown

from coderai.cli.session_factory import build_session_manager, close_session_manager
from coderai.ui.shell.visualize._blocks import render_thinking_block
from coderai.llm import create_openai_client as _core_client
from coderai.soul.session.manager import SessionMessage

console = Console()


def _print_text(text: str) -> None:
    console.print(text)


def _print_markdown(markdown_text: str) -> None:
    console.print(Markdown(markdown_text))


def _permission_replies_for_print(requests: list[dict], *, plan_mode: bool) -> list[dict]:
    """Auto-allow tools in print/exec mode, but fail-closed on Plan Mode mutations."""
    from coderai.soul.approval import apply_auto_approve_to_permission_plan

    plan = {
        "permissions": [
            {"toolCallId": item.get("toolCallId"), "permission": "ask"} for item in requests
        ],
        "askPermissions": list(requests),
    }
    gated = apply_auto_approve_to_permission_plan(plan, plan_mode=plan_mode) or plan
    remaining = {item.get("toolCallId") for item in (gated.get("askPermissions") or [])}
    replies: list[dict] = []
    for item in gated.get("permissions") or []:
        tool_call_id = item.get("toolCallId")
        if tool_call_id in remaining or item.get("permission") == "ask":
            replies.append({"toolCallId": tool_call_id, "permission": "deny"})
        else:
            replies.append({"toolCallId": tool_call_id, "permission": "allow"})
    return replies


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
    if auto_approve:
        try:
            manager.set_yolo(True)
        except Exception:
            pass

    try:
        try:
            from coderai.llm import ensure_oauth_fresh

            await ensure_oauth_fresh()
        except Exception:
            pass
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
                    replies = _permission_replies_for_print(
                        requests,
                        plan_mode=bool(getattr(entry, "plan_mode", False) or plan_mode),
                    )
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
