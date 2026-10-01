"""Tool dispatch preparation and result persistence boundaries."""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from coderai.soul.approval import build_permission_tool_execution
from coderai.soul.session.models import SessionMessage
from coderai.tools.legacy.registry import ToolRegistry
from coderai.utils.common.file_history import GitFileHistory
from coderai.utils.common.message_converter import OpenAIMessageConverter


def prepare_tool_dispatch(
    session_id: str,
    project_root: str,
    tool_calls: list[Any],
    registry: ToolRegistry | None,
    permission_replies: list[dict[str, Any]] | None,
    message_permissions: list[dict[str, Any]] | None,
    pre_tool_outcomes: dict[str, dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[tuple[str, list[dict[str, Any]]]]]:
    """Normalize and partition calls without executing or publishing them."""
    read_only_tool_names = {
        "read",
        "Read",
        "read_file",
        "grep",
        "Grep",
        "glob",
        "Glob",
        "read_media_file",
        "WebSearch",
        "web_search",
        "WebFetch",
        "web_fetch",
        "UnderstandImage",
        "understand_image",
    }

    # Normalize all raw tool calls
    normalized_calls: list[dict[str, Any]] = []
    for raw_tc in tool_calls:
        tc = (
            raw_tc
            if isinstance(raw_tc, dict)
            else {
                "id": getattr(raw_tc, "id", "") or uuid.uuid4().hex,
                "type": "function",
                "function": {
                    "name": getattr(getattr(raw_tc, "function", None), "name", "") or "",
                    "arguments": getattr(getattr(raw_tc, "function", None), "arguments", "") or "",
                },
            }
        )
        tc["id"] = str(tc.get("id") or uuid.uuid4().hex)
        normalized_calls.append(tc)

    # Partition into contiguous execution chunks (parallel vs sequential vs barrier)
    chunks: list[tuple[str, list[dict[str, Any]]]] = []
    current_chunk_kind: str | None = None
    current_chunk: list[dict[str, Any]] = []

    for tc in normalized_calls:
        fn = tc.get("function") or {}
        fn_name = str(fn.get("name", "") if isinstance(fn, dict) else "")
        fn_args_raw = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
        parsed_args: dict[str, Any] = {}
        if isinstance(fn_args_raw, dict):
            parsed_args = fn_args_raw
        elif isinstance(fn_args_raw, str) and fn_args_raw.strip():
            try:
                parsed_args = json.loads(fn_args_raw)
            except Exception:
                parsed_args = {}

        blocked = build_permission_tool_execution(tc, permission_replies, message_permissions)

        from coderai.tools.legacy.types import matches_tool_call_binding, tool_call_binding

        snapshot = (pre_tool_outcomes or {}).get(tc["id"]) or {}
        hook_stop = matches_tool_call_binding(
            snapshot, tool_call_binding(session_id, project_root, tc)
        ) and (snapshot.get("outcome") or {}).get("stop")
        if hook_stop:
            kind = "barrier"
        elif blocked:
            kind = "blocked"
        else:
            tool_def = registry.get(fn_name) if registry else None
            if tool_def:
                mode = tool_def.check_execution_mode(parsed_args)
                if mode == "barrier":
                    kind = "barrier"
                elif mode == "parallel":
                    kind = "parallel"
                else:
                    kind = "sequential"
            elif fn_name in read_only_tool_names:
                kind = "parallel"
            else:
                kind = "sequential"

        if kind == "barrier":
            if current_chunk and current_chunk_kind is not None:
                chunks.append((current_chunk_kind, current_chunk))
                current_chunk = []
                current_chunk_kind = None
            chunks.append(("barrier", [tc]))
        elif current_chunk_kind is None or current_chunk_kind == kind:
            current_chunk_kind = kind
            current_chunk.append(tc)
        else:
            chunks.append((current_chunk_kind, current_chunk))
            current_chunk_kind = kind
            current_chunk = [tc]

    if current_chunk and current_chunk_kind is not None:
        chunks.append((current_chunk_kind, current_chunk))

    return normalized_calls, chunks


class ToolResultHost(Protocol):
    @property
    def message_converter(self) -> OpenAIMessageConverter: ...

    @property
    def file_history(self) -> GitFileHistory: ...

    def set_plan_mode(self, session_id: str, enabled: bool) -> bool: ...

    def _build_tool_message(
        self,
        session_id: str,
        tool_call_id: str,
        content: str,
        tool_function: Any = None,
        tool_meta: dict[str, Any] | None = None,
    ) -> SessionMessage: ...

    def _build_message(
        self,
        session_id: str,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> SessionMessage: ...

    def _append_message(self, message: SessionMessage) -> None: ...

    def on_assistant_message(self, message: SessionMessage, is_tool: bool) -> None: ...


def persist_tool_execution(
    host: ToolResultHost,
    session_id: str,
    tool_calls: list[Any],
    chunk_tcs: list[dict[str, Any]],
    execution: dict[str, Any],
    target_path: str | None,
) -> None:
    """Persist one result, then advisories and checkpoints in their original order."""
    result = execution["result"]
    exec_tc_id = execution["toolCallId"]
    result_meta = result.get("metadata") if isinstance(result, dict) else None
    if isinstance(result_meta, dict) and result_meta.get("exitPlanMode"):
        host.set_plan_mode(session_id, False)
    if isinstance(result_meta, dict) and result_meta.get("enterPlanMode"):
        host.set_plan_mode(session_id, True)

    tool_fn = host.message_converter.find_tool_function(tool_calls, exec_tc_id)
    if not tool_fn:
        matching = [t for t in chunk_tcs if t.get("id") == exec_tc_id]
        if matching:
            tool_fn = matching[0].get("function")

    tool_msg = host._build_tool_message(
        session_id,
        exec_tc_id,
        execution["content"],
        tool_fn,
        tool_meta=result_meta if isinstance(result_meta, dict) else None,
    )
    host._append_message(tool_msg)
    host.on_assistant_message(tool_msg, True)

    follow_up_messages: list[SessionMessage] = []
    for follow_up in result.get("followUpMessages") or []:
        role = (
            follow_up.get("role", "user")
            if isinstance(follow_up, dict)
            else getattr(follow_up, "role", "user")
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
        if content:
            follow_up_messages.append(
                host._build_message(
                    session_id,
                    "user",
                    content,
                    meta={"contentParams": content_params, "advisoryRole": role}
                    if content_params
                    else {"advisoryRole": role},
                )
            )

    for fum in follow_up_messages:
        host._append_message(fum)

    # Record post-mutation checkpoint for file-editing tools
    tp = target_path
    if tp:
        try:
            host.file_history.record_checkpoint(session_id, [tp], "After tool execution")
        except Exception:
            pass
