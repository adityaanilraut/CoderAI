"""SessionManager — UI-agnostic agent loop.

Port of deepcode core/src/session.ts, sized for a Python CLI:

    stream -> tool_calls -> permissions -> execute -> loop

Persistence: JSONL messages + a sessions index under
`~/.coderai/projects/<projectCode>/`, with token-threshold compaction,
isolated GitFileHistory checkpoint-based undo, subagent orchestration, and Plan Mode gating.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import pathlib
import re
import uuid
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from coderai.core.common.file_history import GitFileHistory
from coderai.core.common.llm_error import describe_llm_error
from coderai.core.common.message_converter import OpenAIMessageConverter
from coderai.core.common.openai_thinking import build_thinking_request_options
from coderai.core.mcp import McpManager
from coderai.core.permissions import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    build_permission_tool_execution,
    compute_tool_call_permissions,
    resolve_snippet_file_path,
)
from coderai.core.prompt import (
    get_compact_prompt,
    get_compact_prompt_token_threshold,
    get_plan_mode_prompt,
    get_runtime_context,
    get_system_prompt,
    get_tools,
)
from coderai.core.state import clear_session_state, rebuild_session_state_from_history
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.types import ToolExecutionHooks

MAX_ITERATIONS = 50
MAX_SESSION_ENTRIES = 50


def get_project_code(project_root: str) -> str:
    norm = str(pathlib.Path(project_root).resolve())
    h = hashlib.sha256(norm.encode()).hexdigest()[:16]
    base = pathlib.Path(norm).name[:32].replace(" ", "-") or "project"
    return f"{base}-{h}"


@dataclass
class SessionMessage:
    id: str
    session_id: str
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[Any] | None = None
    tool_call_id: str | None = None
    thinking: str | None = None
    compacted: bool = False
    visible: bool = True
    create_time: str = ""
    update_time: str = ""
    meta: dict[str, Any] | None = None


@dataclass
class SessionEntry:
    id: str
    summary: str = ""
    assistant_reply: str | None = None
    assistant_thinking: str | None = None
    assistant_refusal: str | None = None
    tool_calls: list[Any] | None = None
    status: str = "pending"
    fail_reason: str | None = None
    ask_permissions: list[dict[str, Any]] | None = None
    usage: dict[str, Any] | None = None
    usage_per_model: dict[str, Any] | None = None
    active_tokens: int = 0
    create_time: str = ""
    update_time: str = ""
    plan_mode: bool = False
    fork_of: str | None = None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class SessionManager:
    def __init__(
        self,
        *,
        project_root: str,
        create_openai_client: Callable[[], dict[str, Any]],
        get_resolved_settings: Callable[[], dict[str, Any]],
        render_markdown: Callable[[str], str] | None = None,
        on_assistant_message: Callable[[SessionMessage, bool], None] | None = None,
        on_session_entry_updated: Callable[[SessionEntry], None] | None = None,
        on_stream_chunk: Callable[[str], None] | None = None,
        on_llm_stream_progress: Callable[[dict[str, Any]], None] | None = None,
        non_interactive: bool = False,
    ) -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.create_openai_client = create_openai_client
        self.get_resolved_settings = get_resolved_settings
        self.render_markdown = render_markdown or (lambda t: t)
        self.on_assistant_message = on_assistant_message or (lambda m, c: None)
        self.on_session_entry_updated = on_session_entry_updated
        self.on_stream_chunk = on_stream_chunk
        self.on_llm_stream_progress = on_llm_stream_progress
        self.non_interactive = non_interactive
        self.tool_executor = ToolExecutor(self.project_root, create_openai_client)
        self.mcp_manager = McpManager()
        self.mcp_manager.prepare(self.get_resolved_settings().get("mcpServers"))
        self.mcp_tool_definitions: list[dict[str, Any]] = []
        self.message_converter = OpenAIMessageConverter()
        self._active_session_id: str | None = None
        self._override_model: str | None = None
        self.session_controllers: dict[str, asyncio.Event] = {}
        self.file_history = GitFileHistory(
            self.project_root, str(self._storage()["project_dir"] / "file-history" / ".git")
        )

    def set_model(self, model_name: str) -> None:
        self._override_model = model_name.strip() if model_name else None

    def get_active_model(self) -> str:
        if self._override_model:
            return self._override_model
        return str(self.get_resolved_settings().get("model") or "gpt-4o")

    def get_diff(self, session_id: str | None = None, from_checkpoint: str | None = None) -> str:
        sid = session_id or self._active_session_id
        if not sid:
            return ""
        return self.file_history.get_diff(sid, from_checkpoint=from_checkpoint)

    # ---- storage ----

    def _storage(self) -> dict[str, pathlib.Path]:
        code = get_project_code(self.project_root)
        project_dir = pathlib.Path.home() / ".coderai" / "projects" / code
        return {
            "project_dir": project_dir,
            "index_path": project_dir / "sessions-index.json",
        }

    def _messages_path(self, session_id: str) -> pathlib.Path:
        return self._storage()["project_dir"] / f"{session_id}.jsonl"

    def _ensure_dir(self) -> pathlib.Path:
        d = self._storage()["project_dir"]
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_index(self) -> dict[str, Any]:
        idx_path = self._storage()["index_path"]
        if not idx_path.exists():
            return {"version": 1, "entries": [], "originalPath": self.project_root}
        try:
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            return {
                "version": 1,
                "entries": data.get("entries") or [],
                "originalPath": self.project_root,
            }
        except Exception:
            return {"version": 1, "entries": [], "originalPath": self.project_root}

    def _save_index(self, index: dict[str, Any]) -> None:
        self._ensure_dir()
        self._storage()["index_path"].write_text(json.dumps(index, indent=2), encoding="utf-8")

    def _append_message(self, message: SessionMessage) -> None:
        messages = self.list_session_messages(message.session_id)
        messages.append(message)
        self._save_messages(message.session_id, messages)

    def _save_messages(self, session_id: str, messages: list[SessionMessage]) -> None:
        self._ensure_dir()
        with open(self._messages_path(session_id), "w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(self._serialize_message(m), ensure_ascii=False) + "\n")

    def list_session_messages(self, session_id: str) -> list[SessionMessage]:
        path = self._messages_path(session_id)
        if not path.exists():
            return []
        messages: list[SessionMessage] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                messages.append(self._deserialize_message(json.loads(line), session_id))
            except (ValueError, TypeError):
                continue
        return messages

    def _serialize_message(self, m: SessionMessage) -> dict[str, Any]:
        return {
            "id": m.id,
            "sessionId": m.session_id,
            "role": m.role,
            "content": m.content,
            "toolCalls": m.tool_calls,
            "toolCallId": m.tool_call_id,
            "thinking": m.thinking,
            "compacted": m.compacted,
            "visible": m.visible,
            "createTime": m.create_time,
            "updateTime": m.update_time,
            "meta": m.meta,
        }

    def _deserialize_message(self, d: dict[str, Any], session_id: str) -> SessionMessage:
        return SessionMessage(
            id=d.get("id") or uuid.uuid4().hex,
            session_id=session_id,
            role=d.get("role") or "user",
            content=d.get("content") or "",
            tool_calls=d.get("toolCalls"),
            tool_call_id=d.get("toolCallId"),
            thinking=d.get("thinking"),
            compacted=bool(d.get("compacted")),
            visible=d.get("visible") is not False,
            create_time=d.get("createTime") or "",
            update_time=d.get("updateTime") or "",
            meta=d.get("meta"),
        )

    def _update_entry(
        self, session_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> bool:
        index = self._load_index()
        for i, entry in enumerate(index["entries"]):
            if entry.get("id") == session_id:
                index["entries"][i] = mutate(entry)
                self._save_index(index)
                if self.on_session_entry_updated:
                    self.on_session_entry_updated(_entry_from_dict(index["entries"][i]))
                return True
        return False

    def _get_entry(self, session_id: str) -> dict[str, Any] | None:
        for entry in self._load_index()["entries"]:
            if entry.get("id") == session_id:
                return entry
        return None

    # ---- builders ----

    def _build_message(
        self, session_id: str, role: str, content: str, **kwargs: Any
    ) -> SessionMessage:
        now = _now()
        return SessionMessage(
            id=uuid.uuid4().hex,
            session_id=session_id,
            role=role,
            content=content,
            create_time=now,
            update_time=now,
            **kwargs,
        )

    def _build_assistant(
        self,
        session_id: str,
        content: str,
        tool_calls: list[Any] | None,
        thinking: str | None = None,
    ) -> SessionMessage:
        return self._build_message(
            session_id, "assistant", content, tool_calls=tool_calls, thinking=thinking
        )

    def _build_tool_message(
        self,
        session_id: str,
        tool_call_id: str,
        content: str,
        tool_function: Any = None,
    ) -> SessionMessage:
        now = _now()
        is_invisible = _is_invisible_execution(content)
        params_md = _build_tool_params_snippet(tool_function)
        result_md = _build_tool_result_snippet(content)
        return SessionMessage(
            id=uuid.uuid4().hex,
            session_id=session_id,
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            compacted=False,
            visible=not is_invisible,
            create_time=now,
            update_time=now,
            meta={"function": tool_function, "paramsMd": params_md, "resultMd": result_md},
        )

    # ---- lifecycle ----

    def interrupt_session(self, session_id: str) -> None:
        """Interrupt and cancel a running session."""
        ctrl = self.session_controllers.get(session_id)
        if ctrl:
            ctrl.set()
        else:
            event = asyncio.Event()
            event.set()
            self.session_controllers[session_id] = event
        self._update_entry(
            session_id,
            lambda e: {
                **e,
                "status": "interrupted",
                "failReason": "interrupted",
                "updateTime": _now(),
            },
        )

    def is_interrupted(self, session_id: str) -> bool:
        ctrl = self.session_controllers.get(session_id)
        if ctrl and ctrl.is_set():
            return True
        entry = self._get_entry(session_id)
        return bool(entry and entry.get("status") in ("interrupted", "failed"))

    async def create_session(self, user_prompt: str, plan_mode: bool = False) -> str:
        session_id = uuid.uuid4().hex
        summary = (user_prompt or "[Image Prompt]")[:100]
        now = _now()
        index = self._load_index()
        entry = {
            "id": session_id,
            "summary": summary,
            "assistantReply": None,
            "assistantThinking": None,
            "assistantRefusal": None,
            "toolCalls": None,
            "status": "pending",
            "failReason": None,
            "usage": None,
            "usagePerModel": None,
            "activeTokens": 0,
            "createTime": now,
            "updateTime": now,
            "planMode": plan_mode,
        }
        index["entries"].append(entry)
        index["entries"] = sorted(
            index["entries"], key=lambda e: e.get("updateTime", ""), reverse=True
        )[:MAX_SESSION_ENTRIES]
        self._save_index(index)

        # File history session checkpoint
        self.file_history.ensure_session(session_id)
        ckpt_res = self.file_history.record_tracked_files_checkpoint(
            session_id, "User prompt checkpoint"
        )

        model = self.get_active_model()
        self._append_message(self._build_message(session_id, "system", get_system_prompt()))
        self._append_message(
            self._build_message(session_id, "system", get_runtime_context(self.project_root, model))
        )
        if plan_mode:
            self._append_message(
                self._build_message(
                    session_id,
                    "system",
                    get_plan_mode_prompt(),
                    meta={"isPlanMode": True},
                )
            )
        self._append_message(
            self._build_message(
                session_id,
                "user",
                user_prompt,
                meta={
                    "checkpointHash": ckpt_res.checkpoint_hash,
                    "userPrompt": {"planMode": plan_mode},
                },
            )
        )
        self._active_session_id = session_id
        await self._activate(session_id)
        return session_id

    async def reply_session(
        self,
        session_id: str,
        user_prompt: str | None = None,
        permission_replies: list[dict[str, Any]] | None = None,
        plan_mode: bool | None = None,
    ) -> None:
        entry = self._get_entry(session_id)
        if not entry:
            await self.create_session(user_prompt or "", plan_mode=bool(plan_mode))
            return

        if plan_mode is not None:
            prev_mode = bool(entry.get("planMode"))
            if prev_mode != plan_mode:
                self._update_entry(
                    session_id,
                    lambda e: {**e, "planMode": plan_mode, "updateTime": _now()},
                )
                if plan_mode:
                    self._append_message(
                        self._build_message(
                            session_id,
                            "system",
                            get_plan_mode_prompt(),
                            meta={"isPlanMode": True},
                        )
                    )
                else:
                    self._append_message(
                        self._build_message(
                            session_id,
                            "system",
                            "You have exited Plan Mode. You are now free to execute the plan and perform mutating operations.",
                            meta={"isPlanMode": False},
                        )
                    )

        # Handle /continue without appending redundant user message
        is_continue = user_prompt and user_prompt.strip() == "/continue"

        if permission_replies is not None:
            # If user provided a message alongside permission replies, queue it as deferred prompt
            deferred_prompt = user_prompt if (user_prompt and not is_continue) else None
            await self._activate(
                session_id, permission_replies=permission_replies, deferred_prompt=deferred_prompt
            )
            return

        if user_prompt and not is_continue:
            self.file_history.ensure_session(session_id)
            ckpt_res = self.file_history.record_tracked_files_checkpoint(
                session_id, "User prompt checkpoint"
            )
            curr_entry = self._get_entry(session_id)
            curr_mode = bool(curr_entry.get("planMode")) if curr_entry else False
            self._append_message(
                self._build_message(
                    session_id,
                    "user",
                    user_prompt,
                    meta={
                        "checkpointHash": ckpt_res.checkpoint_hash,
                        "userPrompt": {"planMode": curr_mode},
                    },
                )
            )

        self._active_session_id = session_id
        await self._activate(session_id)

    async def _activate(
        self,
        session_id: str,
        permission_replies: list[dict[str, Any]] | None = None,
        deferred_prompt: str | None = None,
    ) -> None:
        client_info = self.create_openai_client()
        client = client_info.get("client")
        model = self.get_active_model()
        base_url = client_info.get("baseURL")
        temperature = client_info.get("temperature")
        thinking_enabled = bool(client_info.get("thinkingEnabled"))
        reasoning_effort = client_info.get("reasoningEffort") or "max"
        settings = self.get_resolved_settings()

        abort_event = asyncio.Event()
        self.session_controllers[session_id] = abort_event

        messages = self.list_session_messages(session_id)
        rebuild_session_state_from_history(
            session_id, [self._serialize_message(m) for m in messages]
        )

        self._update_entry(
            session_id,
            lambda e: {**e, "status": "processing", "failReason": None, "updateTime": _now()},
        )

        if client is None:
            self._update_entry(
                session_id,
                lambda e: {
                    **e,
                    "status": "failed",
                    "failReason": "API key not found",
                    "updateTime": _now(),
                },
            )
            self.on_assistant_message(
                self._build_message(
                    session_id,
                    "assistant",
                    "API key not found. Set your API key in .env (e.g. OPENAI_API_KEY=...), export it in your shell, or configure ~/.coderai/settings.json.",
                ),
                False,
            )
            self.session_controllers.pop(session_id, None)
            return

        try:
            for iteration in range(MAX_ITERATIONS):
                if self.is_interrupted(session_id):
                    return

                messages = self.list_session_messages(session_id)

                # Check for trailing pending tool calls (e.g. from permission pause or resume)
                pending_info = self.message_converter.get_trailing_pending_tool_call_message(
                    messages
                )
                if pending_info.get("toolCalls"):
                    last_msg = pending_info.get("message")
                    tool_calls = pending_info.get("toolCalls") or []
                    msg_perms = (last_msg.meta or {}).get("askPermissions") if last_msg else None

                    waiting = await self._append_tool_messages(
                        session_id,
                        tool_calls,
                        permission_replies=permission_replies,
                        message_permissions=msg_perms,
                    )
                    permission_replies = None

                    # If there was a deferred prompt attached to the permission response, append it now
                    if deferred_prompt:
                        self.file_history.ensure_session(session_id)
                        ckpt = self.file_history.record_tracked_files_checkpoint(
                            session_id, "User prompt checkpoint"
                        )
                        self._append_message(
                            self._build_message(
                                session_id,
                                "user",
                                deferred_prompt,
                                meta={"checkpointHash": ckpt.checkpoint_hash},
                            )
                        )
                        deferred_prompt = None

                    if self.is_interrupted(session_id):
                        return

                    if waiting:
                        self._update_entry(
                            session_id,
                            lambda e: {
                                **e,
                                "toolCalls": tool_calls,
                                "status": "waiting_for_user",
                                "updateTime": _now(),
                            },
                        )
                        return
                    continue

                # Auto-compaction check before making LLM completion request
                current_entry = self._get_entry(session_id) or {}
                active_tokens = current_entry.get("activeTokens", 0)
                auto_compact_thresh = settings.get(
                    "autoCompactWindow"
                ) or get_compact_prompt_token_threshold(model)
                if active_tokens > auto_compact_thresh:
                    compact_notice = self._build_assistant(
                        session_id,
                        "The conversation is getting long, compacting...",
                        None,
                    )
                    compact_notice.meta = {"asThinking": True}
                    self.on_assistant_message(compact_notice, False)
                    await self._compact_session(session_id)
                    messages = self.list_session_messages(session_id)

                # Prepare tools and messages for LLM request
                tools = get_tools(
                    {
                        "model": model,
                        "nonInteractive": self.non_interactive,
                    },
                    external_tools=self.mcp_tool_definitions,
                )
                converted = self.message_converter.convert_session_messages(
                    messages, model, thinking_enabled=thinking_enabled
                )
                request: dict[str, Any] = {
                    "model": model,
                    "messages": converted,
                    "tools": tools if tools else None,
                }
                if temperature is not None:
                    request["temperature"] = temperature
                request.update(
                    build_thinking_request_options(
                        thinking_enabled,
                        base_url=base_url,
                        reasoning_effort=reasoning_effort,
                        model=model,
                        has_tools=bool(request.get("tools")),
                    )
                )
                if not request.get("tools"):
                    request.pop("tools", None)

                # Execute LLM completion with stream tracking
                try:
                    response = await self._create_completion(client, request)
                except Exception as err:
                    if self.is_interrupted(session_id):
                        return
                    err_str = describe_llm_error(err)
                    self._update_entry(
                        session_id,
                        lambda entry: {
                            **entry,
                            "status": "failed",
                            "failReason": err_str,
                            "updateTime": _now(),
                        },
                    )
                    self.on_assistant_message(
                        self._build_message(
                            session_id,
                            "assistant",
                            f"Request failed: {err_str}",
                        ),
                        False,
                    )
                    return

                if self.is_interrupted(session_id):
                    return

                choice = (response.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                content = msg.get("content") or ""
                raw_tool_calls = msg.get("tool_calls")
                thinking = msg.get("reasoning_content")
                refusal = msg.get("refusal")
                tool_calls = _normalize_tool_calls(raw_tool_calls)

                usage = response.get("usage")
                total_active = _total_tokens(usage)

                # Build and record assistant turn
                assistant_msg = self._build_assistant(session_id, content, tool_calls, thinking)

                curr_entry = self._get_entry(session_id) or {}
                is_plan = bool(curr_entry.get("planMode"))
                forced_scopes = PLAN_MODE_FORCE_ASK_SCOPES if is_plan else None

                perm_plan = (
                    compute_tool_call_permissions(
                        session_id=session_id,
                        project_root=self.project_root,
                        tool_calls=tool_calls,
                        settings=settings.get("permissions") or {},
                        force_ask_scopes=forced_scopes,
                    )
                    if tool_calls
                    else None
                )

                if perm_plan and perm_plan.get("permissions"):
                    assistant_msg.meta = {
                        **(assistant_msg.meta or {}),
                        "permissions": perm_plan["permissions"],
                        "askPermissions": perm_plan.get("askPermissions"),
                    }

                self._append_message(assistant_msg)
                self.on_assistant_message(assistant_msg, True)

                waiting_for_user = False
                if tool_calls:
                    if perm_plan and perm_plan.get("askPermissions"):
                        # Permission required from user
                        self._update_entry(
                            session_id,
                            lambda e: {
                                **e,
                                "assistantReply": content,
                                "assistantThinking": thinking,
                                "assistantRefusal": refusal,
                                "toolCalls": tool_calls,
                                "usage": _accumulate_usage(e.get("usage"), usage),
                                "usagePerModel": _accumulate_usage_per_model(
                                    e.get("usagePerModel"), model, usage
                                ),
                                "activeTokens": total_active or e.get("activeTokens", 0),
                                "status": "ask_permission",
                                "failReason": None,
                                "askPermissions": perm_plan["askPermissions"],
                                "updateTime": _now(),
                            },
                        )
                        return

                    # Execute allowed tool calls
                    waiting_for_user = await self._append_tool_messages(
                        session_id,
                        tool_calls,
                        permission_replies=None,
                        message_permissions=perm_plan.get("permissions") if perm_plan else None,
                    )

                if self.is_interrupted(session_id):
                    return

                new_status = (
                    "failed"
                    if refusal
                    else "waiting_for_user"
                    if waiting_for_user
                    else "processing"
                    if tool_calls
                    else "completed"
                )

                self._update_entry(
                    session_id,
                    lambda e: {
                        **e,
                        "assistantReply": content,
                        "assistantThinking": thinking,
                        "assistantRefusal": refusal,
                        "toolCalls": tool_calls,
                        "usage": _accumulate_usage(e.get("usage"), usage),
                        "usagePerModel": _accumulate_usage_per_model(
                            e.get("usagePerModel"), model, usage
                        ),
                        "activeTokens": total_active or e.get("activeTokens", 0),
                        "status": new_status,
                        "failReason": refusal if refusal else e.get("failReason"),
                        "askPermissions": None,
                        "updateTime": _now(),
                    },
                )

                if refusal or waiting_for_user:
                    return

                if not tool_calls:
                    return

            # Max iterations reached
            self._update_entry(
                session_id,
                lambda e: {
                    **e,
                    "status": "completed",
                    "updateTime": _now(),
                },
            )
            continuation_msg = self._build_assistant(
                session_id,
                "The AI agent has taken several steps but hasn't reached a conclusion yet. Do you want to continue?",
                None,
            )
            self.on_assistant_message(continuation_msg, False)

        except asyncio.CancelledError:
            self._update_entry(
                session_id,
                lambda e: {
                    **e,
                    "status": "interrupted",
                    "failReason": "interrupted",
                    "updateTime": _now(),
                },
            )
        finally:
            self.session_controllers.pop(session_id, None)

    async def _append_tool_messages(
        self,
        session_id: str,
        tool_calls: list[Any],
        permission_replies: list[dict[str, Any]] | None = None,
        message_permissions: list[dict[str, Any]] | None = None,
    ) -> bool:
        waiting = False
        ctrl = self.session_controllers.get(session_id)

        for raw_tc in tool_calls:
            if ctrl and ctrl.is_set():
                break

            tc = (
                raw_tc
                if isinstance(raw_tc, dict)
                else {
                    "id": getattr(raw_tc, "id", "") or uuid.uuid4().hex,
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(raw_tc, "function", None), "name", "") or "",
                        "arguments": getattr(getattr(raw_tc, "function", None), "arguments", "")
                        or "",
                    },
                }
            )
            tc_id = str(tc.get("id") or uuid.uuid4().hex)
            tc["id"] = tc_id

            blocked = build_permission_tool_execution(tc, permission_replies, message_permissions)
            if blocked:
                tool_msg = self._build_tool_message(
                    session_id,
                    tc_id,
                    blocked["content"],
                    tc.get("function"),
                )
                self._append_message(tool_msg)
                self.on_assistant_message(tool_msg, True)
                continue

            # Record pre-mutation checkpoint for file-editing tools
            func = tc.get("function") if isinstance(tc, dict) else None
            name = str(func.get("name", "")) if isinstance(func, dict) else ""
            target_path = None
            if name in ("edit", "Edit", "write", "Write"):
                try:
                    target_path = _resolve_target_file_path(session_id, self.project_root, tc)
                    if target_path:
                        self.file_history.record_checkpoint(
                            session_id, [target_path], f"Before {name} tool execution"
                        )
                except Exception:
                    pass

            def _on_before_file_mutation(fp: str) -> None:
                if fp:
                    try:
                        self.file_history.record_checkpoint(
                            session_id, [fp], f"Before {name} tool execution"
                        )
                    except Exception:
                        pass

            def _on_after_file_mutation(fp: str) -> None:
                if fp:
                    try:
                        self.file_history.record_checkpoint(
                            session_id, [fp], f"After {name} tool execution"
                        )
                    except Exception:
                        pass

            hooks = ToolExecutionHooks(
                on_before_file_mutation=_on_before_file_mutation,
                on_after_file_mutation=_on_after_file_mutation,
                should_stop=lambda: self.is_interrupted(session_id),
            )

            executions = await self.tool_executor.execute_tool_calls(session_id, [tc], hooks=hooks)
            follow_up_messages: list[SessionMessage] = []

            for execution in executions:
                result = execution["result"]
                if result.get("awaitUserResponse") is True:
                    waiting = True

                tool_fn = self.message_converter.find_tool_function(
                    tool_calls, execution["toolCallId"]
                ) or tc.get("function")
                tool_msg = self._build_tool_message(
                    session_id,
                    execution["toolCallId"],
                    execution["content"],
                    tool_fn,
                )
                self._append_message(tool_msg)
                self.on_assistant_message(tool_msg, True)

                for follow_up in result.get("followUpMessages") or []:
                    role = (
                        follow_up.get("role", "system")
                        if isinstance(follow_up, dict)
                        else getattr(follow_up, "role", "system")
                    )
                    content = (
                        follow_up.get("content", "")
                        if isinstance(follow_up, dict)
                        else getattr(follow_up, "content", "")
                    )
                    content_params = (
                        follow_up.get("contentParams")
                        if isinstance(follow_up, dict)
                        else getattr(follow_up, "content_params", None)
                    )
                    if role == "system":
                        follow_up_messages.append(
                            self._build_message(
                                session_id,
                                "system",
                                content,
                                meta={"contentParams": content_params} if content_params else None,
                            )
                        )

            for fum in follow_up_messages:
                self._append_message(fum)

            # Record post-mutation checkpoint for file-editing tools
            if target_path:
                try:
                    self.file_history.record_checkpoint(
                        session_id, [target_path], f"After {name} tool execution"
                    )
                except Exception:
                    pass

        return waiting

    async def _create_completion(self, client: Any, request: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            _call_stream_or_sync, client, request, self.on_stream_chunk, self.on_llm_stream_progress
        )

    async def _compact_session(self, session_id: str) -> None:
        client_info = self.create_openai_client()
        client = client_info.get("client")
        if client is None:
            return
        messages = [m for m in self.list_session_messages(session_id) if not m.compacted]
        start = next((i for i, m in enumerate(messages) if m.role != "system"), -1)
        if start == -1:
            return
        end = -1
        search_start = start + (len(messages) - start) * 2 // 3
        for i in range(max(search_start, start), len(messages)):
            if messages[i].role != "tool":
                end = i
                break
        if end == -1 or end <= start:
            return
        prompt = get_compact_prompt(messages[start:end])
        model = self.get_active_model()
        response = await self._create_completion(
            client, {"model": model, "messages": [{"role": "user", "content": prompt}]}
        )
        raw = (response.get("choices") or [{}])[0].get("message") or {}
        raw_summary = str(raw.get("content") or "").strip()
        summary = re.sub(
            r"<analysis>[\s\S]*?</analysis>", "", raw_summary, flags=re.IGNORECASE
        ).strip()
        now = _now()
        for i in range(start, end):
            messages[i] = _copy_with(messages[i], compacted=True, update_time=now)
        summary_message = self._build_message(
            session_id,
            "system",
            f"There are earlier parts of the conversation. Here is a summary:\n\n{summary}",
            meta={"isSummary": True},
        )
        summary_message.visible = False
        messages.insert(end, summary_message)
        self._save_messages(session_id, messages)

    # ---- queries, delete, fork & undo ----

    def delete_session(self, session_id: str) -> bool:
        """Remove session messages and entry from index."""
        index = self._load_index()
        initial_len = len(index["entries"])
        index["entries"] = [e for e in index["entries"] if e.get("id") != session_id]
        if len(index["entries"]) == initial_len:
            return False

        self._save_index(index)
        msg_file = self._messages_path(session_id)
        if msg_file.exists():
            try:
                msg_file.unlink()
            except Exception:
                pass

        clear_session_state(session_id)
        self.session_controllers.pop(session_id, None)
        return True

    def fork_session(self, source_session_id: str) -> str | None:
        """Fork an existing session into a new independent session with shared history."""
        src_entry = self._get_entry(source_session_id)
        if not src_entry:
            return None

        forked_id = uuid.uuid4().hex
        now = _now()
        source_messages = self.list_session_messages(source_session_id)

        forked_messages: list[SessionMessage] = []
        for m in source_messages:
            forked_messages.append(
                SessionMessage(
                    id=uuid.uuid4().hex,
                    session_id=forked_id,
                    role=m.role,
                    content=m.content,
                    tool_calls=m.tool_calls,
                    tool_call_id=m.tool_call_id,
                    thinking=m.thinking,
                    compacted=m.compacted,
                    visible=m.visible,
                    create_time=m.create_time or now,
                    update_time=now,
                    meta=m.meta,
                )
            )

        self._save_messages(forked_id, forked_messages)

        index = self._load_index()
        forked_entry = {
            "id": forked_id,
            "summary": f"[Fork] {src_entry.get('summary', '')}",
            "assistantReply": src_entry.get("assistantReply"),
            "assistantThinking": src_entry.get("assistantThinking"),
            "assistantRefusal": None,
            "toolCalls": src_entry.get("toolCalls"),
            "status": "completed",
            "failReason": None,
            "askPermissions": None,
            "usage": None,
            "usagePerModel": None,
            "activeTokens": src_entry.get("activeTokens", 0),
            "createTime": now,
            "updateTime": now,
            "planMode": src_entry.get("planMode", False),
            "forkOf": source_session_id,
        }
        index["entries"].insert(0, forked_entry)
        index["entries"] = index["entries"][:MAX_SESSION_ENTRIES]
        self._save_index(index)

        self.file_history.ensure_session(forked_id)
        return forked_id

    def undo(self, session_id: str) -> bool:
        """Revert files and message history back to previous user prompt checkpoint."""
        messages = self.list_session_messages(session_id)
        user_messages = [m for m in messages if m.role == "user" and not m.compacted and m.visible]
        if not user_messages:
            return False

        latest_user = user_messages[-1]
        checkpoint_hash = (latest_user.meta or {}).get("checkpointHash")
        if not checkpoint_hash:
            checkpoint_hash = self.file_history.get_current_checkpoint_hash(session_id)

        if not checkpoint_hash or not self.file_history.can_restore(session_id, checkpoint_hash):
            return False

        self.file_history.restore(session_id, checkpoint_hash)

        cutoff_idx = next((i for i, m in enumerate(messages) if m.id == latest_user.id), -1)
        if cutoff_idx >= 0:
            retained_messages = messages[:cutoff_idx]
            self._save_messages(session_id, retained_messages)
            rebuild_session_state_from_history(
                session_id, [self._serialize_message(m) for m in retained_messages]
            )

        self._update_entry(
            session_id,
            lambda e: {
                **e,
                "status": "completed",
                "updateTime": _now(),
            },
        )
        return True

    def list_sessions(self) -> list[SessionEntry]:
        return [_entry_from_dict(e) for e in self._load_index()["entries"]]

    def get_session(self, session_id: str) -> SessionEntry | None:
        entry = self._get_entry(session_id)
        return _entry_from_dict(entry) if entry else None

    async def init_mcp_servers(self) -> None:
        await self.mcp_manager.initialize(self.get_resolved_settings().get("mcpServers"))
        self.mcp_tool_definitions = self.mcp_manager.get_mcp_tool_definitions()

    def dispose(self) -> None:
        for event in self.session_controllers.values():
            event.set()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.mcp_manager.disconnect())
            return
        if not loop.is_closed():
            loop.create_task(self.mcp_manager.disconnect())


def _entry_from_dict(d: dict[str, Any]) -> SessionEntry:
    return SessionEntry(
        id=d.get("id", ""),
        summary=d.get("summary", ""),
        assistant_reply=d.get("assistantReply"),
        assistant_thinking=d.get("assistantThinking"),
        assistant_refusal=d.get("assistantRefusal"),
        tool_calls=d.get("toolCalls"),
        status=d.get("status", "pending"),
        fail_reason=d.get("failReason"),
        ask_permissions=d.get("askPermissions"),
        usage=d.get("usage"),
        usage_per_model=d.get("usagePerModel"),
        active_tokens=d.get("activeTokens", 0),
        create_time=d.get("createTime", ""),
        update_time=d.get("updateTime", ""),
        plan_mode=bool(d.get("planMode")),
        fork_of=d.get("forkOf"),
    )


def _normalize_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    result: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            func = tc.get("function") or {}
            tc_id = tc.get("id") or uuid.uuid4().hex
            result.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", "") or "",
                    },
                }
            )
        else:
            tc_id = getattr(tc, "id", "") or uuid.uuid4().hex
            result.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(tc, "function", None), "name", "") or "",
                        "arguments": getattr(getattr(tc, "function", None), "arguments", "") or "",
                    },
                }
            )
    return result or None


def _call_stream_or_sync(
    client: Any,
    request: dict[str, Any],
    on_chunk: Callable[[str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    stream_req = {**request, "stream": True}
    try:
        try:
            resp = client.chat.completions.create(**stream_req)
        except Exception as err:
            err_msg = str(err).lower()
            if "reasoning_effort" in err_msg:
                retry_req = dict(stream_req)
                if "none" in err_msg:
                    retry_req["reasoning_effort"] = "none"
                else:
                    retry_req.pop("reasoning_effort", None)
                    if isinstance(retry_req.get("extra_body"), dict):
                        retry_req["extra_body"].pop("reasoning_effort", None)
                        if not retry_req["extra_body"]:
                            retry_req.pop("extra_body", None)
                resp = client.chat.completions.create(**retry_req)
            else:
                raise

        if hasattr(resp, "choices"):
            return _format_completion_response(resp)

        if hasattr(resp, "__iter__"):
            content_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls_dict: dict[int, dict[str, Any]] = {}
            refusal_parts: list[str] = []
            usage_dict: dict[str, int] = {}
            estimated_tokens = 0

            for chunk in resp:
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    c = choices[0]
                    delta = getattr(c, "delta", None)
                    if delta:
                        delta_content = getattr(delta, "content", None)
                        if delta_content:
                            content_parts.append(delta_content)
                            estimated_tokens += max(1, len(delta_content) // 4)
                            if on_chunk:
                                on_chunk(delta_content)
                            if on_progress:
                                on_progress({"estimatedTokens": estimated_tokens, "type": "update"})

                        delta_thinking = getattr(delta, "reasoning_content", None) or getattr(
                            delta, "thinking", None
                        )
                        if delta_thinking:
                            thinking_parts.append(delta_thinking)
                            estimated_tokens += max(1, len(delta_thinking) // 4)
                            if on_progress:
                                on_progress({"estimatedTokens": estimated_tokens, "type": "update"})

                        delta_refusal = getattr(delta, "refusal", None)
                        if delta_refusal:
                            refusal_parts.append(delta_refusal)

                        delta_tc = getattr(delta, "tool_calls", None)
                        if delta_tc:
                            for tc_delta in delta_tc:
                                idx = getattr(tc_delta, "index", 0)
                                if idx not in tool_calls_dict:
                                    tool_calls_dict[idx] = {
                                        "id": getattr(tc_delta, "id", "") or uuid.uuid4().hex,
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                entry = tool_calls_dict[idx]
                                if getattr(tc_delta, "id", None):
                                    entry["id"] = tc_delta.id
                                func = getattr(tc_delta, "function", None)
                                if func:
                                    if getattr(func, "name", None):
                                        entry["function"]["name"] += func.name
                                    if getattr(func, "arguments", None):
                                        entry["function"]["arguments"] += func.arguments

                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage_dict = {
                        "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(chunk_usage, "completion_tokens", 0),
                        "total_tokens": getattr(chunk_usage, "total_tokens", 0),
                    }

            if on_progress:
                on_progress({"estimatedTokens": estimated_tokens, "type": "end"})

            tool_calls = [tool_calls_dict[i] for i in sorted(tool_calls_dict.keys())] or None
            res: dict[str, Any] = {
                "choices": [
                    {
                        "message": {
                            "content": "".join(content_parts),
                            "tool_calls": tool_calls,
                            "reasoning_content": "".join(thinking_parts) or None,
                            "refusal": "".join(refusal_parts) or None,
                        }
                    }
                ]
            }
            if usage_dict:
                res["usage"] = usage_dict
            return res
    except Exception:
        pass

    return _call_sync(client, request)


def _format_completion_response(resp: Any) -> dict[str, Any]:
    message = getattr(resp.choices[0], "message", None)
    result: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": getattr(message, "content", None) or "",
                    "tool_calls": _pydantic_tool_calls(message),
                    "reasoning_content": getattr(message, "reasoning_content", None),
                    "refusal": getattr(message, "refusal", None),
                }
            }
        ]
    }
    usage = getattr(resp, "usage", None)
    if usage is not None:
        result["usage"] = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
    return result


def _call_sync(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    resp = client.chat.completions.create(**request)
    return _format_completion_response(resp)


def _pydantic_tool_calls(message: Any) -> list[dict[str, Any]] | None:
    raw = getattr(message, "tool_calls", None)
    if not raw:
        return None
    out: list[dict[str, Any]] = []
    for tc in raw:
        func = getattr(tc, "function", None)
        out.append(
            {
                "id": getattr(tc, "id", "") or uuid.uuid4().hex,
                "type": "function",
                "function": {
                    "name": getattr(func, "name", "") or "",
                    "arguments": getattr(func, "arguments", "") or "",
                },
            }
        )
    return out or None


def _accumulate_usage(
    current: dict[str, Any] | None, usage: dict[str, Any] | None
) -> dict[str, Any] | None:
    if usage is None:
        return current
    c = current or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": c.get("prompt_tokens", 0) + usage.get("prompt_tokens", 0),
        "completion_tokens": c.get("completion_tokens", 0) + usage.get("completion_tokens", 0),
        "total_tokens": c.get("total_tokens", 0) + usage.get("total_tokens", 0),
    }


def _accumulate_usage_per_model(
    current: dict[str, Any] | None, model: str, usage: dict[str, Any] | None
) -> dict[str, Any] | None:
    if usage is None or not model:
        return current
    res = dict(current or {})
    res[model] = _accumulate_usage(res.get(model), usage)
    return res


def _total_tokens(usage: dict[str, Any] | None) -> int:
    return usage.get("total_tokens", 0) if usage else 0


def _copy_with(m: SessionMessage, **changes: Any) -> SessionMessage:
    return SessionMessage(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        tool_calls=m.tool_calls,
        tool_call_id=m.tool_call_id,
        thinking=m.thinking,
        compacted=changes.get("compacted", m.compacted),
        visible=m.visible,
        create_time=m.create_time,
        update_time=changes.get("update_time", m.update_time),
        meta=m.meta,
    )


def _resolve_target_file_path(session_id: str, project_root: str, tc: Any) -> str | None:
    if isinstance(tc, dict):
        args_raw = tc.get("function", {}).get("arguments", "{}")
    else:
        func = getattr(tc, "function", None)
        args_raw = getattr(func, "arguments", "{}") if func else "{}"
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except Exception:
        args = {}
    if not isinstance(args, dict):
        return None
    fp = args.get("file_path")
    if isinstance(fp, str) and fp.strip():
        p = pathlib.Path(fp)
        return (
            str(p.resolve()) if p.is_absolute() else str((pathlib.Path(project_root) / p).resolve())
        )
    snippet_id = args.get("snippet_id")
    if isinstance(snippet_id, str) and snippet_id.strip():
        return resolve_snippet_file_path(session_id, snippet_id)
    return None


def _is_invisible_execution(content: str) -> bool:
    try:
        data = json.loads(content)
        return bool(data.get("metadata", {}).get("invisible") is True)
    except Exception:
        return False


def _build_tool_params_snippet(tool_function: Any) -> str:
    if not tool_function:
        return ""
    if isinstance(tool_function, dict):
        name = tool_function.get("name", "")
        args = tool_function.get("arguments", "{}")
    else:
        name = getattr(tool_function, "name", "")
        args = getattr(tool_function, "arguments", "{}")
    return f"`{name}`: {str(args)[:100]}"


def _build_tool_result_snippet(content: str) -> str:
    try:
        data = json.loads(content)
        if data.get("ok"):
            out = str(data.get("output") or "OK")
            return out[:100] + ("..." if len(out) > 100 else "")
        err = str(data.get("error") or "Error")
        return f"Error: {err[:100]}"
    except Exception:
        return content[:100]
