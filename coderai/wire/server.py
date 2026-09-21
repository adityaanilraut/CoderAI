"""Wire-protocol stdio server.

Speaks JSON-RPC 2.0 on stdin/stdout so IDEs and headless clients can drive a
session: ``initialize / prompt / steer / replay / set_plan_mode / cancel``,
plus ``event`` notifications and ``request`` round-trips (approval/question).

Turn execution reuses the public ``SessionManager`` API (``reply_session`` /
``create_session`` / ``respond_permissions``); permission and question pauses
are bridged to wire requests instead of terminal prompts. Deliberately out of
scope (documented, not faked): client-executed external tools (rejected with a
reason at ``initialize``) and wire hook subscriptions (ignored).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from typing import Any

from coderai.wire.jsonrpc import ErrorCodes, Statuses
from coderai.wire.protocol import WIRE_PROTOCOL_VERSION
from coderai.wire.types import (
    ApprovalRequest,
    QuestionItem,
    QuestionOption,
    QuestionRequest,
    is_event,
    is_request,
    serialize_wire_message,
)

IN_METHODS = {"initialize", "prompt", "steer", "replay", "set_plan_mode", "cancel"}
VALID_APPROVAL_RESPONSES = ("approve", "approve_for_session", "reject")


def _success(id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _error(id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message, "data": data}}


def _event(msg: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "event", "params": serialize_wire_message(msg)}


def _request(id: Any, msg: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "request",
        "id": id,
        "params": serialize_wire_message(msg),
    }


_active_wire_server: WireServer | None = None


def get_active_wire_server() -> WireServer | None:
    return _active_wire_server


class WireServer:
    """Serve one session over stdio (one prompt turn at a time)."""

    def __init__(self, mgr: Any, session_id: str | None = None) -> None:
        global _active_wire_server
        _active_wire_server = self
        self._mgr = mgr
        self._session_id = session_id
        self._initialized = False
        self._client_supports_question = False
        self._client_supports_plan_mode = False
        self._write_queue: asyncio.Queue = asyncio.Queue()
        self._pending: dict[str, Any] = {}
        self._turn_task: asyncio.Task | None = None
        self._steers: list[str] = []
        self._cancel_event: asyncio.Event | None = None
        self._hub_queue: Any = None
        self._dispatch_tasks: set[asyncio.Task[Any]] = set()

    # -- serve ------------------------------------------------------------
    async def serve(self) -> int:
        reader_task = asyncio.create_task(self._read_loop())
        writer_task = asyncio.create_task(self._write_loop())
        hub = getattr(self._mgr, "root_wire_hub", None)
        hub_task: asyncio.Task | None = None
        if hub is not None:
            self._hub_queue = hub.subscribe()
            hub_task = asyncio.create_task(self._hub_loop())
        try:
            await reader_task
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self._shutdown_requests()
            for dt in list(self._dispatch_tasks):
                dt.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dt
            if self._turn_task is not None:
                self._turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._turn_task
            if hub_task is not None:
                hub_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hub_task
            if hub is not None and self._hub_queue is not None:
                try:
                    hub.unsubscribe(self._hub_queue)
                except Exception:
                    pass
            self._write_queue.shutdown()
            await writer_task
            global _active_wire_server
            if _active_wire_server is self:
                _active_wire_server = None
        return 0

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                await self._send(_error(None, ErrorCodes.PARSE_ERROR, "Invalid JSON format"))
                continue
            if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
                msg_id = data.get("id") if isinstance(data, dict) else None
                await self._send(_error(msg_id, ErrorCodes.INVALID_REQUEST, "Invalid request"))
                continue
            task = asyncio.create_task(self._dispatch(data))
            task.add_done_callback(self._dispatch_tasks.discard)
            self._dispatch_tasks.add(task)

    async def _write_loop(self) -> None:
        while True:
            try:
                msg = await self._write_queue.get()
            except asyncio.QueueShutDown:
                break
            try:
                sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except Exception:
                break

    async def _hub_loop(self) -> None:
        assert self._hub_queue is not None
        while True:
            try:
                msg = await self._hub_queue.get()
            except asyncio.CancelledError:
                return
            except Exception:
                return
            try:
                if not self._initialized:
                    continue
                if is_event(msg):
                    await self._send(_event(msg))
                # Requests created through the approval runtime are bridged
                # inline by the turn loop; hub copies need no round-trip here.
            except Exception:
                continue

    async def _send(self, msg: dict[str, Any]) -> None:
        try:
            await self._write_queue.put(msg)
        except asyncio.QueueShutDown:
            pass

    # -- dispatch ----------------------------------------------------------
    async def _dispatch(self, data: dict[str, Any]) -> None:
        method = data.get("method")
        msg_id = data.get("id")
        if method is None:
            await self._handle_response(data)
            return
        if method not in IN_METHODS:
            if msg_id is not None:
                await self._send(
                    _error(
                        msg_id, ErrorCodes.METHOD_NOT_FOUND, f"Unexpected method received: {method}"
                    )
                )
            return
        params = data.get("params")
        if method == "prompt":
            if not isinstance(params, dict) or "user_input" not in params:
                if msg_id is not None:
                    await self._send(
                        _error(
                            msg_id,
                            ErrorCodes.INVALID_PARAMS,
                            "Invalid parameters for method `prompt`",
                        )
                    )
                return
        if not isinstance(params, dict):
            params = {}
        handler = {
            "initialize": self._handle_initialize,
            "prompt": self._handle_prompt,
            "steer": self._handle_steer,
            "replay": self._handle_replay,
            "set_plan_mode": self._handle_set_plan_mode,
            "cancel": self._handle_cancel,
        }[method]
        try:
            resp = await handler(msg_id, params)
        except Exception as exc:
            resp = (
                _error(msg_id, ErrorCodes.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
                if msg_id is not None
                else None
            )
        if resp is not None:
            await self._send(resp)

    # -- handlers -----------------------------------------------------------
    async def _handle_initialize(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if self._streaming:
            return _error(msg_id, ErrorCodes.INVALID_STATE, "An agent turn is already in progress")
        from coderai._version import __version__
        from coderai.ui.shell.slash import COMMAND_CATALOG
        from coderai.hooks.config import HOOK_EVENT_TYPES

        rejected: list[dict[str, str]] = []
        for tool in params.get("external_tools") or []:
            name = tool.get("name", "") if isinstance(tool, dict) else ""
            rejected.append(
                {"name": str(name), "reason": "client-executed external tools are not supported"}
            )
        capabilities = params.get("capabilities") or {}
        if isinstance(capabilities, dict):
            self._client_supports_question = bool(capabilities.get("supports_question"))
            self._client_supports_plan_mode = bool(capabilities.get("supports_plan_mode"))
        slash_commands = [
            {"name": cmd.name, "description": cmd.summary, "aliases": list(cmd.aliases)}
            for cmd in COMMAND_CATALOG.values()
        ]
        # Dynamic /skill: + /flow: entries.
        try:
            for skill in self._mgr.list_available_skills(self._session_id):
                name = str(skill.get("name", ""))
                if not name:
                    continue
                slash_commands.append(
                    {
                        "name": f"skill:{name}",
                        "description": str(skill.get("description", "")),
                        "aliases": [],
                    }
                )
                if str(skill.get("type", "standard")).lower() == "flow":
                    slash_commands.append(
                        {
                            "name": f"flow:{name}",
                            "description": str(skill.get("description", "")),
                            "aliases": [],
                        }
                    )
        except Exception:
            pass

        hook_engine = getattr(self._mgr, "hook_engine", None) or getattr(
            self._mgr, "_hook_engine", None
        )
        configured_hooks = getattr(hook_engine, "summary", {}) if hook_engine else {}

        result: dict[str, Any] = {
            "protocol_version": WIRE_PROTOCOL_VERSION,
            "server": {"name": "coderai", "version": __version__},
            "slash_commands": slash_commands,
            "hooks": {
                "supported_events": HOOK_EVENT_TYPES,
                "configured": configured_hooks,
            },
            "capabilities": {"supports_question": True},
        }
        if rejected:
            result["external_tools"] = {"accepted": [], "rejected": rejected}
        self._initialized = True
        return _success(msg_id, result)

    async def _handle_prompt(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if self._streaming:
            return _error(msg_id, ErrorCodes.INVALID_STATE, "An agent turn is already in progress")
        user_input = params.get("user_input", "")
        if isinstance(user_input, list):
            from kosong.message import (
                AudioURLPart,
                ImageURLPart,
                Message as _KMsg,
                TextPart as _KTextPart,
                ThinkPart,
                VideoURLPart,
            )
            from coderai.soul.message import check_message

            _parts = []
            for item in user_input:
                if isinstance(item, dict):
                    t = item.get("type")
                    if t == "text":
                        _parts.append(_KTextPart(text=item.get("text", "")))
                    elif t == "image_url":
                        _parts.append(ImageURLPart.model_validate(item))
                    elif t == "video_url":
                        _parts.append(VideoURLPart.model_validate(item))
                    elif t == "audio_url":
                        _parts.append(AudioURLPart.model_validate(item))
                    elif t == "think":
                        _parts.append(ThinkPart(think=item.get("think") or item.get("text", "")))
            _msg = _KMsg(role="user", content=_parts)
            active_settings = getattr(self._mgr, "get_resolved_settings", lambda: {})() or {}
            client_info = getattr(self._mgr, "create_openai_client", lambda: {})() or {}
            model_caps = set(
                client_info.get("capabilities") or active_settings.get("capabilities") or []
            )
            missing = check_message(_msg, model_caps)
            if missing:
                model_name = (
                    client_info.get("model") or active_settings.get("model") or "scripted_echo"
                )
                missing_str = ", ".join(sorted(missing))
                cap_word = "capability" if len(missing) == 1 else "capabilities"
                return _error(
                    msg_id,
                    ErrorCodes.LLM_NOT_SUPPORTED,
                    f"LLM model '{model_name}' does not support required {cap_word}: {missing_str}.",
                )
        text = user_input if isinstance(user_input, str) else json.dumps(user_input)
        self._cancel_event = asyncio.Event()
        try:
            status = await self._run_turn(text)
        except asyncio.CancelledError:
            return _success(msg_id, {"status": Statuses.CANCELLED})
        except Exception as exc:
            exc_name = type(exc).__name__
            exc_str = str(exc)
            if "LLMNotSet" in exc_name or "LLM is not set" in exc_str or "API key" in exc_str:
                return _error(msg_id, ErrorCodes.LLM_NOT_SET, "LLM is not set")
            if "LLMNotSupported" in exc_name or "does not support required capabilit" in exc_str:
                return _error(msg_id, ErrorCodes.LLM_NOT_SUPPORTED, exc_str)
            if (
                "ChatProviderError" in exc_name
                or "APIStatusError" in exc_name
                or "Invalid echo DSL" in exc_str
                or "Unknown echo DSL" in exc_str
                or "connection" in exc_str.lower()
                or "401" in exc_str
            ):
                return _error(msg_id, ErrorCodes.CHAT_PROVIDER_ERROR, exc_str)
            return _error(msg_id, ErrorCodes.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        finally:
            self._cancel_event = None
        if status == "cancelled":
            return _success(msg_id, {"status": Statuses.CANCELLED})
        if status == "max_steps":
            return _success(msg_id, {"status": Statuses.MAX_STEPS_REACHED})
        return _success(msg_id, {"status": Statuses.FINISHED})

    async def _handle_steer(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if not self._streaming:
            return _error(msg_id, ErrorCodes.INVALID_STATE, "No agent turn is in progress")
        user_input = params.get("user_input", "")
        text = user_input if isinstance(user_input, str) else json.dumps(user_input)
        if text.strip():
            if self._session_id is not None and hasattr(self._mgr, "steer_session"):
                self._mgr.steer_session(self._session_id, text.strip())
            else:
                self._steers.append(text.strip())
        return _success(msg_id, {"status": Statuses.STEERED})

    async def _handle_replay(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if self._streaming:
            return _error(msg_id, ErrorCodes.INVALID_STATE, "An agent turn is already in progress")
        events = 0
        requests = 0
        try:
            from coderai.wire.emitter import get_emitter

            for msg in get_emitter().buffered():
                if is_request(msg):
                    await self._send(_request(getattr(msg, "id", ""), msg))
                    requests += 1
                elif is_event(msg):
                    await self._send(_event(msg))
                    events += 1
        except Exception as exc:
            return _error(msg_id, ErrorCodes.INTERNAL_ERROR, f"Replay failed: {exc}")
        return _success(
            msg_id, {"status": Statuses.FINISHED, "events": events, "requests": requests}
        )

    async def _handle_set_plan_mode(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if self._session_id is None:
            return _error(msg_id, ErrorCodes.INVALID_STATE, "No active session")
        enabled = bool(params.get("enabled", False))
        try:
            entry = self._mgr.get_session(self._session_id)
            if entry is None:
                return _error(msg_id, ErrorCodes.INVALID_STATE, "Session not found")
            self._mgr._update_entry(self._session_id, lambda e: {**e, "planMode": enabled})
            try:
                state = self._mgr.get_session_state(self._session_id)
                state.plan_mode = enabled
                self._mgr._save_session_state(self._session_id)
            except Exception:
                pass
            from coderai.wire.types import StatusUpdate

            await self._send(_event(StatusUpdate(plan_mode=enabled)))
        except Exception as exc:
            return _error(msg_id, ErrorCodes.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return _success(msg_id, {"status": "ok", "plan_mode": enabled})

    async def _handle_cancel(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if not self._streaming:
            return _error(msg_id, ErrorCodes.INVALID_STATE, "No agent turn is in progress")
        try:
            if self._session_id is not None:
                self._mgr.interrupt_session(self._session_id)
            if self._cancel_event is not None:
                self._cancel_event.set()
        except Exception:
            pass
        return _success(msg_id, {})

    async def _handle_response(self, data: dict[str, Any]) -> None:
        msg_id = data.get("id")
        request = self._pending.pop(msg_id, None)
        if request is None:
            return
        if isinstance(request, ApprovalRequest):
            if "error" in data:
                request.resolve("reject")
                return
            result = data.get("result") or {}
            response = str(result.get("response", "reject"))
            if response not in VALID_APPROVAL_RESPONSES:
                response = "reject"
            request.resolve(response, str(result.get("feedback", "")))
        elif isinstance(request, QuestionRequest):
            if "error" in data:
                request.resolve({})
                return
            result = data.get("result") or {}
            answers = result.get("answers")
            request.resolve(dict(answers) if isinstance(answers, dict) else {})
        else:
            try:
                request.resolve("allow", "")
            except Exception:
                pass

    # -- turn loop -----------------------------------------------------------
    @property
    def _streaming(self) -> bool:
        return self._cancel_event is not None

    async def _run_turn(self, text: str) -> str:
        """Drive one prompt to a stable session state; returns a status string."""
        mgr = self._mgr
        forward_task = self._start_event_forwarding()
        try:
            if self._session_id is None:
                self._session_id = await mgr.create_session(text)
            elif text:
                await mgr.reply_session(self._session_id, text)
            else:
                await mgr.reply_session(self._session_id, None)
            if self._cancel_event is not None and self._cancel_event.is_set():
                return "cancelled"
            status = await self._settle_pauses_async()
            if status is not None:
                return status
            # Stable: entry status decides.
            try:
                entry = mgr.get_session(self._session_id)
                entry_status = (entry.status if entry else "") or ""
                fail_reason = (
                    str(getattr(entry, "fail_reason", "") or "") if entry is not None else ""
                ) or ((entry.get("failReason") or "") if isinstance(entry, dict) else "")
            except Exception:
                entry_status, fail_reason = "", ""
            if entry_status in ("interrupted",):
                return "cancelled"
            if entry_status in ("failed",):
                if "API key" in fail_reason:
                    raise RuntimeError(fail_reason or "API key not found")
                raise RuntimeError(fail_reason or f"turn failed ({entry_status})")
            return "finished"
        finally:
            if forward_task is not None:
                forward_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await forward_task

    def _start_event_forwarding(self) -> asyncio.Task | None:
        """Forward live turn events from the process emitter to the client."""
        try:
            from coderai.wire.emitter import get_emitter

            ui_side = get_emitter().ui_side(merge=False)
            # Subscribing replays emitter history; drop it so each turn only
            # forwards the events it actually produces.
            ui_side.drain_nowait()
        except Exception:
            return None

        async def _forward() -> None:
            while True:
                try:
                    msg = await ui_side.receive()
                except asyncio.CancelledError:
                    return
                except Exception:
                    return
                try:
                    if is_request(msg):
                        msg_id = getattr(msg, "id", "") or f"ext_{uuid.uuid4().hex[:8]}"
                        self._pending[msg_id] = msg
                        if isinstance(msg, QuestionRequest) and not self._client_supports_question:
                            from coderai.wire.types import QuestionNotSupported

                            msg.set_exception(QuestionNotSupported())
                        else:
                            await self._send(_request(msg_id, msg))
                    elif is_event(msg):
                        await self._send(_event(msg))
                except Exception:
                    continue

        return asyncio.create_task(_forward())

    async def _settle_pauses_async(self) -> str | None:
        """Bridge ask_permission / ask_user_question to wire requests.

        Returns a status string when the turn is over, or None when stable.
        """
        mgr = self._mgr
        assert self._session_id is not None
        while True:
            entry = mgr.get_session(self._session_id)
            if entry is None:
                return "finished"
            if entry.status == "ask_permission":
                ok = await self._bridge_permissions(entry.ask_permissions or [])
                if not ok:
                    return "cancelled"
                continue
            if entry.status in ("ask_user_question", "waiting_for_user"):
                ok = await self._bridge_question()
                if not ok:
                    return "cancelled"
                continue
            break
        return None

    async def _bridge_permissions(self, items: list[dict[str, Any]]) -> bool:
        """Send one ApprovalRequest per item; apply resolutions; resume on allow."""
        mgr = self._mgr
        assert self._session_id is not None
        wire_requests: list[ApprovalRequest] = []
        for item in items:
            req = ApprovalRequest(
                id=f"apr_{uuid.uuid4().hex[:12]}",
                tool_call_id=str(item.get("toolCallId", "")),
                sender=str(item.get("name", "")),
                action=str(item.get("name", "")),
                description=str(item.get("description", "") or item.get("command", "")),
            )
            wire_requests.append(req)
            self._pending[req.id] = req
            await self._send(_request(req.id, req))
        replies: list[dict[str, Any]] = []
        for req, item in zip(wire_requests, items):
            try:
                response = await req.wait()
            except Exception:
                response = "reject"
            feedback = req.feedback
            if response == "approve":
                replies.append({"toolCallId": item.get("toolCallId"), "permission": "allow"})
            elif response == "approve_for_session":
                replies.append({"toolCallId": item.get("toolCallId"), "permission": "allow"})
                try:
                    self._record_session_allow(item)
                except Exception:
                    pass
            else:
                reply: dict[str, Any] = {
                    "toolCallId": item.get("toolCallId"),
                    "permission": "deny",
                }
                if feedback:
                    reply["feedback"] = feedback
                replies.append(reply)
        try:
            await mgr.respond_permissions(self._session_id, replies)
        except Exception:
            return False
        return True

    def _record_session_allow(self, item: dict[str, Any]) -> None:
        """Best-effort session allow (approve_for_session degrades to allow-once).

        Note: the CLI maps "always" to project allows; the wire
        server has no interactive allowlist UI, so approve_for_session behaves
        as allow-once here. A persistent session allowlist is follow-up work.
        """
        pass

    async def _bridge_question(self) -> bool:
        if not self._client_supports_question:
            # Clients without question support get an empty answer
            # so the agent proceeds with its own assumption.
            return True
        mgr = self._mgr
        assert self._session_id is not None
        questions = self._collect_questions()
        if not questions:
            return True
        req = QuestionRequest(
            id=f"q_{uuid.uuid4().hex[:12]}",
            tool_call_id="",
            questions=[
                QuestionItem(
                    question=str(q.get("question", "")),
                    options=[
                        QuestionOption(
                            label=str(o.get("label", "")),
                            description=str(o.get("description", "")),
                        )
                        for o in (q.get("options") or [])
                        if isinstance(o, dict)
                    ],
                    header=str(q.get("header", "")),
                    multi_select=bool(q.get("multiSelect")),
                )
                for q in questions
                if isinstance(q, dict)
            ],
        )
        self._pending[req.id] = req
        await self._send(_request(req.id, req))
        try:
            answers = await req.wait()
        except Exception:
            answers = {}
        text = self._format_answers(questions, answers)
        try:
            await mgr.reply_session(self._session_id, user_prompt=text)
        except Exception:
            return False
        return True

    def _collect_questions(self) -> list[dict[str, Any]]:
        try:
            messages = self._mgr.list_session_messages(self._session_id)
        except Exception:
            return []
        latest_tool = next(
            (
                m
                for m in reversed(messages)
                if getattr(m, "role", "") == "tool" and not getattr(m, "compacted", False)
            ),
            None,
        )
        if latest_tool is None or not getattr(latest_tool, "content", ""):
            return []
        try:
            payload = json.loads(latest_tool.content)
            if isinstance(payload.get("metadata"), dict):
                questions = payload["metadata"].get("questions") or []
                if isinstance(questions, list):
                    return questions
        except Exception:
            pass
        return []

    @staticmethod
    def _format_answers(questions: list[dict[str, Any]], answers: dict[str, str]) -> str:
        lines = ["<answers>"]
        for index, question in enumerate(questions):
            text = str(question.get("question", "")) if isinstance(question, dict) else ""
            key = str(index)
            answer = answers.get(key, "")
            if not answer and isinstance(question, dict):
                answer = answers.get(str(question.get("question", "")), "")
            lines.append(f"Q{index + 1} {text}: {answer}".rstrip())
        lines.append("</answers>")
        return "\n".join(lines)

    def _shutdown_requests(self) -> None:
        for request in self._pending.values():
            try:
                if getattr(request, "resolved", False):
                    continue
                if isinstance(request, ApprovalRequest):
                    request.resolve("reject")
                elif isinstance(request, QuestionRequest):
                    request.resolve({})
                else:
                    request.resolve("allow", "")
            except Exception:
                continue
        self._pending.clear()


async def run_wire_stdio(mgr: Any, session_id: str | None = None, *, init_mcp: bool = True) -> int:
    """Serve the wire protocol on stdio (``coderai --wire`` entry point)."""
    if init_mcp:
        try:
            await mgr.init_mcp_servers()
        except Exception:
            pass
    server = WireServer(mgr, session_id)
    return await server.serve()
