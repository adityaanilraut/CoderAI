"""Conversation, context, cancellation, and session application service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from coderAI.core.agent_tracker import AgentStatus

if TYPE_CHECKING:
    from coderAI.core.agent_tracker import AgentTracker
    from coderAI.tui.controller import UIBridge

logger = logging.getLogger(__name__)


def _tracker() -> "AgentTracker":
    # Resolved through the controller module at call time so tests patching
    # coderAI.tui.controller.agent_tracker keep working.
    from coderAI.tui import controller

    return controller.agent_tracker


async def _cmd_send_message(server: UIBridge, msg: dict[str, Any]) -> None:
    text = msg.get("text", "")
    async with server._turn_lock:
        server.tick_iteration()
        try:
            result = await server.agent.process_message(text)
            if not getattr(server.agent, "streaming", True):
                content = str((result or {}).get("content", "") or "")
                server.emit("turn", phase="start", reasoningActive=False)
                if content:
                    server.emit("turn", phase="text", delta=content, reasoningActive=False)
                server.emit("turn", phase="end", reasoningActive=False)
        except Exception as e:
            logger.exception("process_message failed")
            server._emit_error(
                "internal",
                str(e),
                hint="See logs on stderr for the full traceback.",
            )
        finally:
            server.emit_status()
            server.emit_ready()


async def _cmd_cancel(server: UIBridge, msg: dict[str, Any]) -> None:
    approvals_cancelled = server._cancel_pending_approvals("cancelled_by_user")
    agent_id = msg.get("agentId")
    if agent_id:
        ok = _tracker().cancel(agent_id)
        if ok:
            server.emit("info", message=f"Cancelled agent {agent_id[-8:]}")
        else:
            server.emit("warning", message=f"No active agent {agent_id}")
    else:
        active = _tracker().get_active()
        _tracker().cancel_all()
        suffix = f" and {approvals_cancelled} pending approval(s)" if approvals_cancelled else ""
        server.emit("info", message=f"Cancelled {len(active)} active agent(s){suffix}")


async def _cmd_tool_approval_resp(server: UIBridge, msg: dict[str, Any]) -> None:
    tool_id = str(msg.get("toolId") or msg.get("id") or "")
    approve = bool(msg.get("approve", False))
    waiter = server._approval_waiters.pop(tool_id, None)
    if waiter is None:
        logger.warning("Late or invalid approval response for tool %s", tool_id)
        server.emit("warning", message="Tool approval response was received too late.")
        return
    # Calling ``set_result`` on an already-resolved or cancelled future would
    # raise ``InvalidStateError``. The waiter can complete out from under us
    # via timeout (``asyncio.wait_for``) or cancellation (``/clear``, ``/exit``)
    # before the UI's response arrives, so check first.
    if waiter.done():
        logger.warning(
            "Approval response for tool %s arrived after waiter resolved (state=%s); ignoring.",
            tool_id,
            "cancelled" if waiter.cancelled() else "done",
        )
        server.emit("warning", message="Tool approval response was received too late.")
        return
    waiter.set_result(approve)


async def _cmd_clear_context(server: UIBridge, msg: dict[str, Any]) -> None:
    from coderAI.tui.serializers import _agent_info_dict

    async with server._turn_lock:
        server.agent.session = None
        server.agent.context_controller.clear()
        server.agent.create_session()
    main_info = getattr(server.agent, "tracker_info", None)
    if main_info is not None:
        _tracker().clear_except({main_info.agent_id})
        main_info.status = AgentStatus.IDLE
        main_info.current_task = ""
        main_info.current_tool = None
        main_info.finished_at = None
        server.emit(
            "agent",
            phase="update",
            info=_agent_info_dict(main_info),
            parentId=main_info.parent_id,
        )
    else:
        _tracker().clear_except()
    server.emit("success", message="Session cleared")
    server.emit_status()


async def _cmd_rewind(server: UIBridge, msg: dict[str, Any]) -> None:
    """Rewind the conversation to before a prior user turn.

    Payload: ``{"turn": int, "files": bool}``. Truncates the session's message
    history back to that turn's checkpoint and, when ``files`` is set, reverts
    file edits made since then. The UI truncates its own timeline in parallel.
    """
    try:
        turn = int(msg.get("turn"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        server.emit("warning", message="Usage: /rewind <turn> [--files]")
        return
    restore_files = bool(msg.get("files", False))

    async with server._turn_lock:
        result = server.agent.rewind_to(turn, restore_files=restore_files)

    if not result.get("ok"):
        server.emit("warning", message=str(result.get("error", "Rewind failed.")))
        return

    parts = [f"Rewound to turn {result['turn']} ({result.get('label', '')})"]
    dropped = int(result.get("dropped_turns", 0) or 0)
    if dropped:
        parts.append(f"dropped {dropped} turn(s)")
    if restore_files:
        parts.append(f"restored {len(result.get('restored_files', []))} file(s)")
    server.emit("success", message=" — ".join(parts))

    file_errors = result.get("file_errors") or []
    if file_errors:
        server.emit(
            "warning",
            message="Some files could not be restored:\n" + "\n".join(file_errors),
        )
    server.emit_status()


async def _cmd_fork_session(server: UIBridge, msg: dict[str, Any]) -> None:
    """Fork the conversation into a new session branch.

    Payload: ``{"turn": Optional[int]}``.
    """
    turn_val = msg.get("turn")
    turn: Optional[int] = None
    if turn_val is not None:
        try:
            turn = int(turn_val)
        except (TypeError, ValueError):
            pass

    async with server._turn_lock:
        try:
            from coderAI.core.services import get_services

            history_mgr = get_services().history
            source_id = server.agent.session.session_id if server.agent.session else None
            new_session = history_mgr.fork_session(
                source_session_id=source_id,
                up_to_turn=turn,
            )
            server.agent.session = new_session
            server.agent._cached_system_prompt = None
            server.agent._refresh_session_system_prompt()
            server.emit(
                "success",
                message=f"Branched into new session: {new_session.session_id} ({new_session.name})",
            )
            server.emit_status()
        except Exception as exc:
            logger.exception("fork_session failed")
            server.emit("warning", message=f"Failed to fork session: {exc}")


async def _cmd_export_session(server: UIBridge, msg: dict[str, Any]) -> None:
    """Export the active session into HTML or Markdown."""
    fmt = str(msg.get("format", "html")).lower()
    path_arg = msg.get("path")
    async with server._turn_lock:
        try:
            from coderAI.tools.session_export import ExportSessionTool

            tool = ExportSessionTool()
            source_id = server.agent.session.session_id if server.agent.session else None
            res = await tool.execute(
                session_id=source_id,
                format=fmt,
                output_path=path_arg,
            )
            if res.get("success"):
                server.emit("success", message=f"Exported session to {res.get('path')}")
            else:
                server.emit("warning", message=str(res.get("error", "Export failed.")))
        except Exception as exc:
            logger.exception("export_session command failed")
            server.emit("warning", message=f"Failed to export session: {exc}")


def _emit_context_state(server: UIBridge) -> None:
    """Emit the pinned-context file listing as a ``context_state`` event.

    Keeps the ``emit("context_state"…)`` literal here so the event-contract
    scanner (which greps ``tui/**`` for emit literals) still finds it.
    """
    context_files = []
    pinned = server.agent.context_controller.pinned_files
    for path_str, content in pinned.items():
        context_files.append({"path": path_str, "size": len(content)})
    server.emit("context_state", files=context_files)


async def _cmd_manage_context(server: UIBridge, msg: dict[str, Any]) -> None:
    action = msg.get("action")
    path = msg.get("path")
    async with server._turn_lock:
        if action == "add":
            if not path:
                server.emit("warning", message="Path required to add to context.")
                return
            success = server.agent.context_controller.add_file(path)
            if success:
                server.emit("success", message=f"Added {path} to pinned context.")
            else:
                server.emit(
                    "warning",
                    message=f"Failed to add {path} to context (may be too large or invalid).",
                )
        elif action == "remove":
            if not path:
                server.emit("warning", message="Path required to remove from context.")
                return
            success = server.agent.context_controller.remove_file(path)
            if success:
                server.emit("success", message=f"Removed {path} from context.")
            else:
                server.emit("warning", message=f"Failed to remove {path} from context.")

        # Emit updated context state
        _emit_context_state(server)
        server.emit_status()


async def _cmd_compact_context(server: UIBridge, msg: dict[str, Any]) -> None:
    try:
        await server.agent.compact_context()
    except Exception as e:
        server._emit_error("internal", f"Compaction failed: {e}")
    else:
        server.emit("success", message="Context compacted")
    server.emit_status()


async def _cmd_get_state(server: UIBridge, msg: dict[str, Any]) -> None:
    from coderAI.tui.serializers import _agent_info_dict

    server.emit_status()
    for info in _tracker().get_all():
        server.emit("agent", phase="update", info=_agent_info_dict(info), parentId=info.parent_id)

    _emit_context_state(server)


async def _cmd_get_tasks(server: UIBridge, _msg: dict[str, Any]) -> None:
    server._emit_tasks_from_disk()


async def _cmd_exit(server: UIBridge, msg: dict[str, Any]) -> None:
    server.emit("goodbye", reason="user")
    server._said_goodbye = True
    server._exit.set()


async def _cmd_cancel_agent(server: UIBridge, msg: dict[str, Any]) -> None:
    """Cancel a specific sub-agent by ID."""
    agent_id = msg.get("agentId") or (msg.get("payload") or {}).get("agentId")
    if not agent_id:
        server.emit("error", category="protocol", message="cancel_agent requires agentId")
        return
    cancelled = _tracker().cancel(agent_id)
    server.emit(
        "success",
        message=f"Sub-agent {agent_id} cancellation {'requested' if cancelled else 'failed (not found)'}",
    )
