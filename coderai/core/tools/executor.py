"""ToolExecutor — dispatches built-in tools with MCP fallback (deepcode executor.ts)."""

from __future__ import annotations

import inspect
import json
from typing import Any
from collections.abc import Callable

from coderai.core.tools import ask_user_question as _ask
from coderai.core.tools import bash as _bash
from coderai.core.tools import edit as _edit
from coderai.core.tools import read as _read
from coderai.core.tools import subagent as _subagent
from coderai.core.tools import understand_image as _image
from coderai.core.tools import update_plan as _plan
from coderai.core.tools import web_search as _search
from coderai.core.tools import write as _write
from coderai.core.tools.types import (
    ToolExecutionContext,
    ToolExecutionHooks,
    ToolResult,
)

BUILT_IN_TOOL_NAME_ALIASES: dict[str, str] = {
    "Bash": "bash",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "task": "Task",
    "Task": "Task",
    "subagent": "Task",
    "SubAgent": "Task",
}

_HANDLERS: dict[str, Callable[..., Any]] = {
    "bash": _bash.handle_bash_tool,
    "read": _read.handle_read_tool,
    "write": _write.handle_write_tool,
    "edit": _edit.handle_edit_tool,
    "AskUserQuestion": _ask.handle_ask_user_question_tool,
    "UpdatePlan": _plan.handle_update_plan_tool,
    "UnderstandImage": _image.handle_understand_image_tool,
    "WebSearch": _search.handle_web_search_tool,
    "Task": _subagent.handle_subagent_tool,
}


class ToolExecutor:
    def __init__(
        self,
        project_root: str,
        create_openai_client: Callable[[], dict[str, Any]] | None = None,
        mcp_manager: Any = None,
    ) -> None:
        self.project_root = project_root
        self.create_openai_client = create_openai_client
        self.mcp_manager = mcp_manager
        self.tool_handlers = dict(_HANDLERS)

    async def execute_tool_calls(
        self,
        session_id: str,
        tool_calls: list[Any],
        hooks: ToolExecutionHooks | dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        parsed_calls: list[dict[str, Any]] = []
        for tc in tool_calls:
            parsed = self._parse_tool_call(tc)
            if parsed:
                parsed_calls.append(parsed)

        should_stop = None
        if hooks:
            should_stop = getattr(hooks, "should_stop", None) or (
                hooks.get("should_stop") if isinstance(hooks, dict) else None
            )

        executions: list[dict[str, Any]] = []
        for tool_call in parsed_calls:
            if should_stop and should_stop():
                break

            result = await self.execute_tool_call(session_id, tool_call, hooks)
            executions.append(
                {
                    "toolCallId": tool_call["id"],
                    "content": self.format_tool_result(result),
                    "result": _result_as_dict(result),
                }
            )

            if should_stop and should_stop():
                break

        return executions

    def _parse_tool_call(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            # Also handle object with id and function attributes
            tc_id = getattr(raw, "id", None)
            func = getattr(raw, "function", None)
            if isinstance(tc_id, str) and func:
                fname = getattr(func, "name", None)
                fargs = getattr(func, "arguments", "")
                if isinstance(fname, str):
                    return {
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": fname,
                            "arguments": fargs if isinstance(fargs, str) else "",
                        },
                    }
            return None

        tc_id = raw.get("id")
        if not isinstance(tc_id, str):
            return None

        func = raw.get("function")
        if not isinstance(func, dict):
            return None

        fname = func.get("name")
        if not isinstance(fname, str):
            return None

        fargs = func.get("arguments", "")
        return {
            "id": tc_id,
            "type": "function",
            "function": {
                "name": fname,
                "arguments": fargs if isinstance(fargs, str) else "",
            },
        }

    async def execute_tool_call(
        self,
        session_id: str,
        tool_call: dict[str, Any],
        hooks: ToolExecutionHooks | dict[str, Any] | None = None,
    ) -> ToolResult:
        tool_name = tool_call["function"]["name"]
        handler_name = BUILT_IN_TOOL_NAME_ALIASES.get(tool_name, tool_name)
        handler = self.tool_handlers.get(handler_name)

        if handler is None:
            if self.mcp_manager is not None and self.mcp_manager.is_mcp_tool(tool_name):
                parsed = self._parse_tool_arguments(tool_call["function"]["arguments"])
                args = parsed["args"] if parsed["ok"] else {}
                try:
                    res = await self.mcp_manager.execute_mcp_tool(tool_name, args)
                    if isinstance(res, ToolResult):
                        return res
                    if isinstance(res, dict):
                        return ToolResult(
                            ok=res.get("ok", True),
                            name=tool_name,
                            output=res.get("output"),
                            error=res.get("error"),
                            metadata=res.get("metadata"),
                        )
                    return ToolResult(ok=True, name=tool_name, output=str(res))
                except Exception as e:
                    return ToolResult(ok=False, name=tool_name, error=str(e))
            return ToolResult(ok=False, name=tool_name, error=f"Unknown tool: {tool_name}")

        parsed_args = self._parse_tool_arguments(tool_call["function"]["arguments"])
        if not parsed_args["ok"]:
            return ToolResult(ok=False, name=tool_name, error=parsed_args["error"])

        context = self._build_execution_context(session_id, tool_call, hooks)

        try:
            if inspect.iscoroutinefunction(handler):
                return await handler(parsed_args["args"], context)
            res = handler(parsed_args["args"], context)
            if inspect.iscoroutine(res):
                return await res
            return res
        except Exception as e:
            return ToolResult(ok=False, name=tool_name, error=str(e))

    def _build_execution_context(
        self,
        session_id: str,
        tool_call: dict[str, Any],
        hooks: ToolExecutionHooks | dict[str, Any] | None = None,
    ) -> ToolExecutionContext:
        def get_hook(name: str) -> Any:
            if not hooks:
                return None
            return getattr(hooks, name, None) or (
                hooks.get(name) if isinstance(hooks, dict) else None
            )

        return ToolExecutionContext(
            session_id=session_id,
            project_root=self.project_root,
            tool_call=tool_call,
            create_openai_client=self.create_openai_client,
            on_process_start=get_hook("on_process_start"),
            on_process_exit=get_hook("on_process_exit"),
            on_process_stdout=get_hook("on_process_stdout"),
            on_process_timeout_control=get_hook("on_process_timeout_control"),
            on_background_process_complete=get_hook("on_background_process_complete"),
            on_before_file_mutation=get_hook("on_before_file_mutation"),
            on_after_file_mutation=get_hook("on_after_file_mutation"),
            on_plugin_rate_limit_exceeded=get_hook("on_plugin_rate_limit_exceeded"),
            bash_timeout_ms=get_hook("bash_timeout_ms"),
            bash_min_timeout_ms=get_hook("bash_min_timeout_ms"),
        )

    def _parse_tool_arguments(self, raw_arguments: str) -> dict[str, Any]:
        if not raw_arguments:
            return {"ok": True, "args": {}}

        try:
            parsed = json.loads(raw_arguments)
        except Exception as e:
            return {
                "ok": False,
                "error": (
                    f"InputParseError: Failed to parse tool arguments: {e}. "
                    "Ensure the tool call arguments are valid JSON. Prefer Edit over Write for large existing-file changes."
                ),
            }

        if not isinstance(parsed, dict) or isinstance(parsed, list):
            return {"ok": False, "error": "InputParseError: Tool arguments must be a JSON object."}

        return {"ok": True, "args": parsed}

    def format_tool_result(self, result: ToolResult) -> str:
        payload: dict[str, Any] = {
            "ok": result.ok,
            "name": result.name,
        }

        if result.output is not None:
            payload["output"] = result.output

        if result.error:
            payload["error"] = result.error

        if result.metadata and isinstance(result.metadata, dict) and len(result.metadata) > 0:
            payload["metadata"] = result.metadata

        if result.await_user_response:
            payload["awaitUserResponse"] = True

        return json.dumps(payload, indent=2)


def _result_as_dict(result: ToolResult) -> dict[str, Any]:
    d: dict[str, Any] = {
        "ok": result.ok,
        "name": result.name,
    }
    if result.output is not None:
        d["output"] = result.output
    if result.error:
        d["error"] = result.error
    if result.metadata:
        d["metadata"] = result.metadata
    if result.await_user_response:
        d["awaitUserResponse"] = True
    if result.follow_up_messages:
        d["followUpMessages"] = [
            m.to_dict() if hasattr(m, "to_dict") else m for m in result.follow_up_messages
        ]
    return d
