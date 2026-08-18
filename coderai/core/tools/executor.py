"""ToolExecutor — dispatches built-in tools with schema validation and MCP fallback (deepcode executor.ts)."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any
from collections.abc import Callable

from coderai.core.common.validate import clean_json_string
from coderai.core.tools.registry import ToolRegistry, get_tool_registry
from coderai.core.tools.types import (
    BackgroundProcessCompletion,
    ProcessTimeoutControl,
    ProcessTimeoutInfo,
    ToolCall,
    ToolCallExecution,
    ToolExecutionContext,
    ToolExecutionFollowUpMessage,
    ToolExecutionHooks,
    ToolExecutionResult,
    ToolResult,
    ValidationError,
)

# Export aliases for compatibility with deepcode
__all__ = [
    "BackgroundProcessCompletion",
    "ProcessTimeoutControl",
    "ProcessTimeoutInfo",
    "ToolCall",
    "ToolCallExecution",
    "ToolExecutionContext",
    "ToolExecutionFollowUpMessage",
    "ToolExecutionHooks",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolResult",
]


class ToolExecutor:
    """Standardized Tool Execution Engine: Validation -> Permission -> Execution -> Truncation -> Context Injection."""

    def __init__(
        self,
        project_root: str,
        create_openai_client: Callable[[], dict[str, Any]] | None = None,
        mcp_manager: Any = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.project_root = project_root
        self.create_openai_client = create_openai_client
        self.mcp_manager = mcp_manager
        self.registry = registry or get_tool_registry()

    @property
    def tool_handlers(self) -> dict[str, Callable[..., Any]]:
        """Backward compatibility dictionary of tool handlers."""
        handlers: dict[str, Callable[..., Any]] = {}
        for tool_def in self.registry.list_tools():
            if tool_def.handler:
                handlers[tool_def.name] = tool_def.handler
                for alias in tool_def.aliases:
                    handlers[alias] = tool_def.handler
        return handlers

    async def execute_tool_calls(
        self,
        session_id: str,
        tool_calls: list[Any],
        hooks: ToolExecutionHooks | dict[str, Any] | None = None,
        parallel: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute a list of tool calls sequentially or in parallel, returning formatted execution payloads."""
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

        if parallel and len(parsed_calls) > 1:
            # Parallel execution path
            async def _run_single(tc: dict[str, Any]) -> dict[str, Any]:
                if should_stop and should_stop():
                    return {
                        "toolCallId": tc["id"],
                        "content": json.dumps(
                            {
                                "ok": False,
                                "name": tc["function"]["name"],
                                "error": "Execution interrupted",
                            }
                        ),
                        "result": {
                            "ok": False,
                            "name": tc["function"]["name"],
                            "error": "Execution interrupted",
                        },
                    }
                res = await self.execute_tool_call(session_id, tc, hooks)
                return {
                    "toolCallId": tc["id"],
                    "content": self.format_tool_result(res),
                    "result": _result_as_dict(res),
                }

            tasks = [_run_single(tc) for tc in parsed_calls]
            executions = await asyncio.gather(*tasks, return_exceptions=False)
            return list(executions)

        # Sequential execution path
        executions_list: list[dict[str, Any]] = []
        for tool_call in parsed_calls:
            if should_stop and should_stop():
                break

            result = await self.execute_tool_call(session_id, tool_call, hooks)
            executions_list.append(
                {
                    "toolCallId": tool_call["id"],
                    "content": self.format_tool_result(result),
                    "result": _result_as_dict(result),
                }
            )

            if should_stop and should_stop():
                break

        return executions_list

    def _parse_tool_call(self, raw: Any) -> dict[str, Any] | None:
        """Parse raw tool call structure from LLM output into normalized dict."""
        if not isinstance(raw, dict):
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
        """Execute a single tool call through the 7-stage lifecycle with structured error recovery."""
        tool_name = tool_call["function"]["name"]
        raw_args_str = tool_call["function"].get("arguments", "")

        # 1. Parse JSON arguments
        parsed = self._parse_tool_arguments(raw_args_str)
        if not parsed["ok"]:
            return ToolResult(ok=False, name=tool_name, error=parsed["error"])
        raw_args = parsed["args"]

        # 2. Check built-in ToolRegistry
        tool_def = self.registry.get(tool_name)
        if tool_def is not None:
            # Strict schema validation
            try:
                validated_args = self.registry.validate_arguments(tool_def.name, raw_args)
            except ValidationError as val_err:
                return ToolResult(
                    ok=False,
                    name=tool_name,
                    error=f"ValidationError: {val_err}",
                )

            # Build execution context
            context = self._build_execution_context(session_id, tool_call, hooks)

            # Execute handler
            try:
                handler = tool_def.handler
                if handler is None:
                    return ToolResult(
                        ok=False,
                        name=tool_name,
                        error=f"Tool '{tool_name}' has no registered handler.",
                    )

                if inspect.iscoroutinefunction(handler):
                    res = await handler(validated_args, context)
                else:
                    res = handler(validated_args, context)
                    if inspect.iscoroutine(res):
                        res = await res

                if isinstance(res, ToolResult):
                    return res
                if isinstance(res, dict):
                    return ToolResult(
                        ok=res.get("ok", True),
                        name=tool_name,
                        output=res.get("output"),
                        error=res.get("error"),
                        metadata=res.get("metadata"),
                        await_user_response=bool(res.get("awaitUserResponse", False)),
                    )
                return ToolResult(ok=True, name=tool_name, output=str(res))

            except Exception as e:
                return ToolResult(ok=False, name=tool_name, error=f"ToolExecutionError: {e}")

        # 3. Check MCP Manager fallback
        if self.mcp_manager is not None and self.mcp_manager.is_mcp_tool(tool_name):
            try:
                res = await self.mcp_manager.execute_mcp_tool(tool_name, raw_args)
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
                return ToolResult(ok=False, name=tool_name, error=f"McpToolExecutionError: {e}")

        # 4. Unknown tool fallback
        return ToolResult(ok=False, name=tool_name, error=f"Unknown tool: {tool_name}")

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
        """Parse raw arguments string into JSON object with clean error messaging."""
        if not raw_arguments:
            return {"ok": True, "args": {}}

        cleaned = (
            clean_json_string(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        )
        try:
            parsed = json.loads(cleaned)
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
        """Format ToolResult into structured JSON string payload for model context injection."""
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
