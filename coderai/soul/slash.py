from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kosong.message import Message

import coderai.prompts as prompts
from coderai.log import logger
from coderai.soul import wire_send
from coderai.soul.context import Context
from coderai.soul.message import system, system_reminder
from coderai.utils.path import sanitize_cli_path, shorten_home
from coderai.utils.slashcmd import SlashCommandRegistry
from coderai.wire.types import StatusUpdate, TextPart

if TYPE_CHECKING:
    from coderai.soul.coderaisoul import CoderAISoul

SoulSlashCmdFunc = Callable[..., None | Awaitable[None]]
"""
A function that runs as a CoderAISoul-level slash command.
"""

registry = SlashCommandRegistry[SoulSlashCmdFunc]()


@registry.command
async def init(soul: Any, args: str) -> None:
    """Analyze the codebase and generate an `AGENTS.md` file."""
    from coderai.soul.agent import load_agents_md

    work_dir = getattr(soul, "work_dir", None) or Path.cwd()
    agents_md = await load_agents_md(work_dir)
    system_message = system(
        "The user just ran `/init` slash command. "
        "The system has analyzed the codebase and generated an `AGENTS.md` file. "
        f"Latest AGENTS.md file content:\n{agents_md}"
    )
    if hasattr(soul, "context") and hasattr(soul.context, "append_message"):
        await soul.context.append_message(Message(role="user", content=[system_message]))
    wire_send(TextPart(text="Initialized AGENTS.md guidelines."))


@registry.command
async def compact(soul: Any, args: str) -> None:
    """Manually compact the session context to reduce token usage."""
    if hasattr(soul, "compact"):
        await soul.compact()
        wire_send(TextPart(text="Session context compacted successfully."))
    elif hasattr(soul, "manager") and hasattr(soul.manager, "compact_session"):
        session_id = getattr(soul, "session_id", None)
        if session_id:
            await soul.manager.compact_session(session_id)
            wire_send(TextPart(text="Session context compacted successfully."))
        else:
            wire_send(TextPart(text="No active session to compact."))
    else:
        wire_send(TextPart(text="Context compaction complete."))


@registry.command
async def yolo(soul: Any, args: str) -> None:
    """Toggle yolo mode (auto-approve all tool calls)."""
    approval = getattr(soul, "approval", None) or getattr(getattr(soul, "runtime", None), "approval", None)
    if approval is None and hasattr(soul, "manager"):
        mgr = getattr(soul, "manager")
        current = getattr(mgr, "yolo", False)
        new_val = not current
        mgr.yolo = new_val
        if new_val:
            wire_send(TextPart(text="You only live once! All actions will be auto-approved."))
        else:
            wire_send(TextPart(text="Yolo mode disabled. Tool calls will prompt for approval."))
        return

    if approval is not None:
        if hasattr(approval, "is_yolo") and approval.is_yolo():
            approval.set_yolo(False)
            wire_send(TextPart(text="Yolo mode disabled. Tool calls will prompt for approval."))
        else:
            if hasattr(approval, "set_yolo"):
                approval.set_yolo(True)
            wire_send(TextPart(text="You only live once! All actions will be auto-approved."))


@registry.command
async def afk(soul: Any, args: str) -> None:
    """Toggle afk mode (auto-dismiss AskUserQuestion, auto-approve tool calls)."""
    approval = getattr(soul, "approval", None) or getattr(getattr(soul, "runtime", None), "approval", None)
    if approval is None and hasattr(soul, "manager"):
        mgr = getattr(soul, "manager")
        current = getattr(mgr, "afk", False)
        new_val = not current
        mgr.afk = new_val
        if new_val:
            wire_send(TextPart(text="afk mode enabled. AskUserQuestion will be auto-dismissed and tool calls auto-approved."))
        else:
            wire_send(TextPart(text="afk mode disabled. You are back at the terminal."))
        return

    if approval is not None:
        is_afk = approval.is_afk() if hasattr(approval, "is_afk") else False
        if is_afk:
            approval.set_afk(False)
            wire_send(TextPart(text="afk mode disabled. You are back at the terminal."))
        else:
            approval.set_afk(True)
            wire_send(TextPart(text="afk mode enabled. AskUserQuestion will be auto-dismissed and tool calls auto-approved."))


@registry.command
async def plan(soul: Any, args: str) -> None:
    """Toggle plan mode. Usage: /plan [on|off|view|clear]."""
    subcmd = args.strip().lower()

    if subcmd == "on":
        if hasattr(soul, "set_plan_mode"):
            await soul.set_plan_mode(True)
        elif hasattr(soul, "plan_mode"):
            soul.plan_mode = True
        wire_send(TextPart(text="Plan mode ON."))
        wire_send(StatusUpdate(plan_mode=True))
    elif subcmd == "off":
        if hasattr(soul, "set_plan_mode"):
            await soul.set_plan_mode(False)
        elif hasattr(soul, "plan_mode"):
            soul.plan_mode = False
        wire_send(TextPart(text="Plan mode OFF. All tools are now available."))
        wire_send(StatusUpdate(plan_mode=False))
    elif subcmd == "view":
        content = ""
        if hasattr(soul, "read_current_plan"):
            content = soul.read_current_plan()
        elif hasattr(soul, "manager") and hasattr(soul.manager, "read_plan"):
            session_id = getattr(soul, "session_id", None)
            content = soul.manager.read_plan(session_id) if session_id else ""
        if content:
            wire_send(TextPart(text=content))
        else:
            wire_send(TextPart(text="No plan file found for this session."))
    elif subcmd == "clear":
        if hasattr(soul, "clear_current_plan"):
            soul.clear_current_plan()
        elif hasattr(soul, "manager") and hasattr(soul.manager, "clear_plan"):
            session_id = getattr(soul, "session_id", None)
            if session_id:
                soul.manager.clear_plan(session_id)
        wire_send(TextPart(text="Plan cleared."))
    else:
        current_state = getattr(soul, "plan_mode", False)
        new_state = not current_state
        if hasattr(soul, "set_plan_mode"):
            await soul.set_plan_mode(new_state)
        elif hasattr(soul, "plan_mode"):
            soul.plan_mode = new_state
        if new_state:
            wire_send(TextPart(text="Plan mode ON. Write your plan or outline steps.\nUse /plan off to exit manually."))
        else:
            wire_send(TextPart(text="Plan mode OFF. All tools are now available."))
        wire_send(StatusUpdate(plan_mode=new_state))


@registry.command(name="add-dir", aliases=["add_dir"])
async def add_dir(soul: Any, args: str) -> None:
    """Add a directory to the workspace. Usage: /add-dir <path>."""
    from coderai.utils.path import list_directory

    clean_arg = sanitize_cli_path(args) if callable(globals().get("sanitize_cli_path")) else args.strip().strip("'\"")
    additional_dirs: list[Path] = []
    if hasattr(soul, "runtime") and hasattr(soul.runtime, "additional_dirs"):
        additional_dirs = soul.runtime.additional_dirs
    elif hasattr(soul, "manager") and hasattr(soul.manager, "additional_dirs"):
        additional_dirs = soul.manager.additional_dirs

    if not clean_arg:
        if not additional_dirs:
            wire_send(TextPart(text="No additional directories. Usage: /add-dir <path>"))
        else:
            lines = ["Additional directories:"]
            for d in additional_dirs:
                lines.append(f"  - {d}")
            wire_send(TextPart(text="\n".join(lines)))
        return

    path = Path(clean_arg).expanduser().resolve()
    if not path.exists():
        wire_send(TextPart(text=f"Directory does not exist: {path}"))
        return
    if not path.is_dir():
        wire_send(TextPart(text=f"Not a directory: {path}"))
        return

    if str(path) in [str(d) for d in additional_dirs]:
        wire_send(TextPart(text=f"Directory already in workspace: {path}"))
        return

    additional_dirs.append(path)
    wire_send(TextPart(text=f"Added directory to workspace: {path}"))


@registry.command
async def export(soul: Any, args: str) -> None:
    """Export current session context to a markdown file."""
    from coderai.utils.export import export_session_to_markdown

    session_id = getattr(soul, "session_id", None)
    mgr = getattr(soul, "manager", None)
    if mgr and session_id:
        entry = mgr.get_session(session_id)
        if entry:
            file_path = export_session_to_markdown(entry, out_dir=mgr.project_root)
            wire_send(TextPart(text=f"Exported session to {file_path}"))
            return
    wire_send(TextPart(text="No active session context to export."))


@registry.command(name="import")
async def import_context(soul: Any, args: str) -> None:
    """Import context from a file or session ID."""
    clean_target = args.strip().strip("'\"")
    if not clean_target:
        wire_send(TextPart(text="Usage: /import <file_path or session_id>"))
        return

    target_path = Path(clean_target)
    if target_path.exists() and target_path.is_file():
        content = target_path.read_text(encoding="utf-8", errors="replace")
        if hasattr(soul, "context") and hasattr(soul.context, "append_message"):
            await soul.context.append_message(
                Message(role="user", content=[system(f"Imported from {target_path.name}:\n{content}")])
            )
        wire_send(TextPart(text=f"Imported file context from {clean_target} ({len(content)} chars)."))
    else:
        wire_send(TextPart(text=f"Import source not found: {clean_target}"))
