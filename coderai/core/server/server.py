"""Headless JSON-RPC 2.0 Server & IDE Companion Bridge (deepcode editor companion protocol)."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from coderai._version import __version__
from coderai.core.common.model_capabilities import CURATED_MODELS
from coderai.core.openai_client import create_openai_client as _core_client
from coderai.core.prompt import list_skills
from coderai.core.server.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    SERVER_NOT_INITIALIZED,
    format_error,
    format_notification,
    format_response,
    parse_message,
)
from coderai.core.session import SessionManager, SessionMessage
from coderai.core.settings import resolve_current_settings


class CoderAIServer:
    """Headless JSON-RPC 2.0 Server exposing CoderAI agent loop and tools to IDEs and editors."""

    def __init__(
        self,
        project_root: str = ".",
        model: str | None = None,
        session_manager: SessionManager | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.project_root = project_root
        self.event_sink = event_sink
        self.initialized = False
        self.running = False
        self.active_turn_session_id: str | None = None

        if session_manager is not None:
            self.session_manager = session_manager
        else:
            self.session_manager = self._build_session_manager(model)

    def set_event_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Configure callback for emitting server notification events."""
        self.event_sink = sink

    def emit_event(self, method: str, params: dict[str, Any]) -> None:
        """Emit a JSON-RPC 2.0 notification event to the client."""
        if self.event_sink:
            notif = format_notification(method, params)
            self.event_sink(notif)

    def _build_session_manager(self, model: str | None) -> SessionManager:
        resolved = resolve_current_settings(self.project_root)
        if model:
            resolved["model"] = model

        def get_settings() -> dict[str, Any]:
            return resolved

        mgr: SessionManager | None = None

        def create_client() -> dict[str, Any]:
            active_model = mgr.get_active_model() if mgr is not None else model
            return _core_client(self.project_root, model_override=active_model)

        def on_assistant_msg(message: SessionMessage, should_connect: bool) -> None:
            self._handle_assistant_message(message)

        def on_stream_chunk(chunk: str) -> None:
            if self.active_turn_session_id:
                self.emit_event(
                    "stream_chunk",
                    {
                        "sessionId": self.active_turn_session_id,
                        "chunk": chunk,
                        "type": "content",
                    },
                )

        mgr = SessionManager(
            project_root=self.project_root,
            create_openai_client=create_client,
            get_resolved_settings=get_settings,
            on_assistant_message=on_assistant_msg,
            on_stream_chunk=on_stream_chunk,
        )
        if model:
            mgr.set_model(model)
        return mgr

    def _handle_assistant_message(self, message: SessionMessage) -> None:
        sess_id = message.session_id or self.active_turn_session_id or ""

        # Tool execution result
        if message.role == "tool":
            ok = True
            output = ""
            error = None
            metadata = None
            name = "tool"
            tool_call_id = ""

            try:
                payload = json.loads(message.content or "{}")
                name = str(payload.get("name") or "tool")
                ok = payload.get("ok") is not False
                output = payload.get("output") or ""
                error = payload.get("error")
                metadata = payload.get("metadata")
            except Exception:
                output = message.content or ""

            self.emit_event(
                "tool_result",
                {
                    "sessionId": sess_id,
                    "toolCallId": tool_call_id,
                    "name": name,
                    "ok": ok,
                    "output": output,
                    "error": error,
                    "metadata": metadata,
                },
            )
            return

        # Thinking trace
        if message.thinking:
            self.emit_event(
                "stream_chunk",
                {
                    "sessionId": sess_id,
                    "chunk": message.thinking,
                    "type": "thinking",
                },
            )

        # Tool invocation requests
        if message.tool_calls:
            for tc in message.tool_calls:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                fn_name = ""
                fn_args: Any = {}
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    raw_args = fn.get("arguments", "{}")
                    try:
                        fn_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        fn_args = raw_args
                else:
                    fn = getattr(tc, "function", None)
                    if fn:
                        fn_name = getattr(fn, "name", "")
                        raw_args = getattr(fn, "arguments", "{}")
                        try:
                            fn_args = (
                                json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                            )
                        except Exception:
                            fn_args = raw_args

                self.emit_event(
                    "tool_call",
                    {
                        "sessionId": sess_id,
                        "toolCallId": tc_id,
                        "name": fn_name,
                        "arguments": fn_args,
                    },
                )

    def _check_pending_interactions(self, session_id: str) -> None:
        entry = self.session_manager.get_session(session_id)
        if not entry:
            return

        if entry.status == "ask_permission" and entry.ask_permissions:
            self.emit_event(
                "ask_permission",
                {
                    "sessionId": session_id,
                    "requests": entry.ask_permissions,
                },
            )
        elif entry.status in ("ask_user_question", "waiting_for_user"):
            messages = self.session_manager.list_session_messages(session_id)
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

            self.emit_event(
                "ask_question",
                {
                    "sessionId": session_id,
                    "questions": questions,
                },
            )

    async def handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch and process a parsed JSON-RPC request dictionary."""
        req_id = payload.get("id")
        method = payload.get("method", "")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return format_error(req_id, INVALID_PARAMS, "Invalid params: expected dict object")

        # 1. Lifecycle: initialize
        if method == "initialize":
            self.initialized = True
            await self.session_manager.init_mcp_servers()
            return format_response(
                req_id,
                {
                    "serverInfo": {
                        "name": "coderai-server",
                        "version": __version__,
                    },
                    "capabilities": {
                        "streaming": True,
                        "tools": True,
                        "skills": True,
                        "mcp": True,
                        "planMode": True,
                        "checkpoints": True,
                    },
                    "projectRoot": self.project_root,
                    "activeModel": self.session_manager.get_active_model(),
                },
            )

        if method == "ping":
            return format_response(req_id, {"pong": True})

        if method == "shutdown":
            self.running = False
            self.session_manager.dispose()
            return format_response(req_id, {"status": "ok"})

        if not self.initialized:
            return format_error(
                req_id,
                SERVER_NOT_INITIALIZED,
                "Server is not initialized. Send 'initialize' request first.",
            )

        # 2. Session Management
        if method in ("session/create", "session.create"):
            prompt = params.get("prompt", "")
            plan_mode = bool(params.get("planMode", False))
            skills = params.get("skills")
            model_override = params.get("model")
            if model_override:
                self.session_manager.set_model(model_override)

            if prompt:
                self.active_turn_session_id = None
                self.emit_event("turn_start", {"sessionId": "", "turnIndex": 1})
                session_id = await self.session_manager.create_session(
                    prompt, plan_mode=plan_mode, skills=skills
                )
                self.active_turn_session_id = session_id
                entry = self.session_manager.get_session(session_id)
                self.emit_event(
                    "turn_finish",
                    {
                        "sessionId": session_id,
                        "turnIndex": 1,
                        "status": entry.status if entry else "completed",
                        "activeTokens": entry.active_tokens if entry else 0,
                    },
                )
                self._check_pending_interactions(session_id)
            else:
                session_id = self.session_manager._create_empty_session(plan_mode=plan_mode)
                entry = self.session_manager.get_session(session_id)

            return format_response(
                req_id,
                {
                    "sessionId": session_id,
                    "status": entry.status if entry else "ready",
                    "activeTokens": entry.active_tokens if entry else 0,
                    "planMode": entry.plan_mode if entry else plan_mode,
                },
            )

        if method in ("session/prompt", "session.prompt"):
            session_id = str(params.get("sessionId", ""))
            prompt = str(params.get("prompt", ""))
            plan_mode_val = bool(params["planMode"]) if "planMode" in params else None
            skills = params.get("skills")

            if not session_id or self.session_manager.get_session(session_id) is None:
                return format_error(req_id, INVALID_PARAMS, f"Session '{session_id}' not found")

            self.active_turn_session_id = session_id
            messages_list = self.session_manager.list_session_messages(session_id)
            turn_idx = sum(1 for m in messages_list if m.role == "user") + 1
            self.emit_event("turn_start", {"sessionId": session_id, "turnIndex": turn_idx})

            await self.session_manager.reply_session(
                session_id,
                user_prompt=prompt,
                plan_mode=plan_mode_val,
                skills=skills,
            )

            entry = self.session_manager.get_session(session_id)
            self.emit_event(
                "turn_finish",
                {
                    "sessionId": session_id,
                    "turnIndex": turn_idx,
                    "status": entry.status if entry else "completed",
                    "activeTokens": entry.active_tokens if entry else 0,
                },
            )
            self._check_pending_interactions(session_id)
            return format_response(
                req_id,
                {
                    "sessionId": session_id,
                    "status": entry.status if entry else "completed",
                    "activeTokens": entry.active_tokens if entry else 0,
                },
            )

        if method in ("session/reply", "session.reply"):
            session_id = str(params.get("sessionId", ""))
            prompt = params.get("prompt")
            permission_replies = params.get("permissionReplies")
            plan_mode_val = bool(params["planMode"]) if "planMode" in params else None

            if not session_id or self.session_manager.get_session(session_id) is None:
                return format_error(req_id, INVALID_PARAMS, f"Session '{session_id}' not found")

            self.active_turn_session_id = session_id
            await self.session_manager.reply_session(
                session_id,
                user_prompt=prompt,
                permission_replies=permission_replies,
                plan_mode=plan_mode_val,
            )

            entry = self.session_manager.get_session(session_id)
            self._check_pending_interactions(session_id)
            return format_response(
                req_id,
                {
                    "sessionId": session_id,
                    "status": entry.status if entry else "completed",
                    "activeTokens": entry.active_tokens if entry else 0,
                },
            )

        if method in ("session/list", "session.list"):
            sessions = self.session_manager.list_sessions()
            return format_response(req_id, {"sessions": [s.to_dict() for s in sessions]})

        if method in ("session/get", "session.get"):
            session_id = str(params.get("sessionId", ""))
            entry = self.session_manager.get_session(session_id)
            if not entry:
                return format_error(req_id, INVALID_PARAMS, f"Session '{session_id}' not found")
            messages = self.session_manager.list_session_messages(session_id)
            return format_response(
                req_id,
                {
                    "session": entry.to_dict(),
                    "messages": [m.to_dict() for m in messages],
                },
            )

        if method in ("session/delete", "session.delete"):
            session_id = str(params.get("sessionId", ""))
            deleted = self.session_manager.delete_session(session_id)
            return format_response(req_id, {"deleted": deleted})

        if method in ("session/fork", "session.fork"):
            session_id = str(params.get("sessionId", ""))
            forked = self.session_manager.fork_session(session_id)
            return format_response(req_id, {"forkedSessionId": forked})

        if method in ("session/compact", "session.compact"):
            session_id = str(params.get("sessionId", ""))
            await self.session_manager.compact_session(session_id)
            entry = self.session_manager.get_session(session_id)
            return format_response(
                req_id,
                {
                    "sessionId": session_id,
                    "activeTokens": entry.active_tokens if entry else 0,
                },
            )

        if method in ("session/undo", "session.undo"):
            session_id = str(params.get("sessionId", ""))
            target_msg_id = params.get("targetMessageId")
            mode = str(params.get("mode", "restore_both"))
            ok = self.session_manager.undo(session_id, target_message_id=target_msg_id, mode=mode)
            return format_response(req_id, {"ok": ok})

        if method in ("session/diff", "session.diff"):
            session_id = str(params.get("sessionId", ""))
            diff_text = self.session_manager.get_diff(session_id)
            return format_response(req_id, {"diff": diff_text})

        if method in ("session/interrupt", "session.interrupt"):
            session_id = str(params.get("sessionId", ""))
            self.session_manager.interrupt_session(session_id)
            return format_response(req_id, {"interrupted": True})

        # 3. Model & MCP & Skills
        if method in ("model/list", "model.list"):
            models_data = [
                {"name": name, "description": desc, "category": cat}
                for name, desc, cat in CURATED_MODELS
            ]
            return format_response(
                req_id,
                {
                    "models": models_data,
                    "activeModel": self.session_manager.get_active_model(),
                },
            )

        if method in ("model/get", "model.get"):
            return format_response(req_id, {"model": self.session_manager.get_active_model()})

        if method in ("model/set", "model.set"):
            new_model = str(params.get("model", "")).strip()
            if not new_model:
                return format_error(req_id, INVALID_PARAMS, "model parameter required")
            self.session_manager.set_model(new_model)
            return format_response(req_id, {"model": self.session_manager.get_active_model()})

        if method in ("mcp/status", "mcp.status"):
            statuses = [
                s.to_dict()
                for s in getattr(self.session_manager.mcp_manager, "server_statuses", [])
            ]
            return format_response(req_id, {"servers": statuses})

        if method in ("mcp/reconnect", "mcp.reconnect"):
            server_name = str(params.get("serverName", "")).strip()
            if not server_name:
                return format_error(req_id, INVALID_PARAMS, "serverName parameter required")
            ok = await self.session_manager.mcp_manager.reconnect(server_name)
            self.session_manager._refresh_mcp_tool_definitions()
            return format_response(req_id, {"ok": ok})

        if method in ("skills/list", "skills.list"):
            skills = list_skills(self.project_root)
            return format_response(req_id, {"skills": skills})

        return format_error(req_id, METHOD_NOT_FOUND, f"Method '{method}' not found")

    async def run_stdio(self) -> None:
        """Run the JSON-RPC stdio transport loop reading from stdin and writing to stdout."""
        self.running = True

        def _write_json(obj: dict[str, Any]) -> None:
            line = json.dumps(obj)
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

        self.set_event_sink(_write_json)

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while self.running:
            try:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break
                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                is_valid, parsed, err_obj = parse_message(line_str)
                if err_obj:
                    _write_json(err_obj)
                    continue

                if is_valid and parsed is not None:
                    # Check if it is a notification (no id)
                    if "id" not in parsed:
                        if parsed.get("method") == "shutdown":
                            self.running = False
                            break
                        continue

                    # Process request
                    response = await self.handle_request(parsed)
                    _write_json(response)
            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            except Exception as e:
                _write_json(format_error(None, INTERNAL_ERROR, f"Internal server error: {e}"))
