from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    Optional,
    TypeVar,
    Union,
    cast,
)

from kosong.tooling import (
    CallableTool,
    CallableTool2,
    ToolError,
    ToolOk,
)
from kosong.tooling.mcp import convert_mcp_content

from coderai.utils.logging import logger
from coderai.wire.types import (
    AudioURLPart,
    ContentPart,
    ImageURLPart,
    TextPart,
    ToolCallRequest,
    ToolReturnValue,
    VideoURLPart,
)
from coderai.soul.tool_context import (
    get_current_tool_call_or_none,
    get_session_id,
    set_session_id,
)

if TYPE_CHECKING:
    import fastmcp
    import mcp
    from fastmcp.client.transports import ClientTransport

ToolType = Union[CallableTool, CallableTool2[Any]]
ToolCallKey = tuple[str, str]
T = TypeVar("T", bound=ToolType)
T_transport = TypeVar("T_transport", bound="ClientTransport")


def _get_session_id() -> str:
    return get_session_id()


def _trace_id_kwargs() -> dict[str, str]:
    """``trace_id`` telemetry kwargs for the current request, empty when unavailable."""
    from coderai.telemetry import get_current_trace_id

    tid = get_current_trace_id()
    if tid:
        return {"trace_id": tid}
    return {}


def _args_hash(canonical_args: str) -> str:
    """Stable 8-char hash of canonical tool-call arguments."""
    import hashlib

    return hashlib.sha256(canonical_args.encode()).hexdigest()[:8]


_REMINDER_TEXT_1 = (
    "\n\n<system-reminder>\n"
    "You are repeating the exact same tool call with identical parameters."
    " Please carefully analyze the previous result. If the task is not yet complete,"
    " try a different method or parameters instead of repeating the same call."
    "\n</system-reminder>"
)


def _make_reminder_text_2(tool_name: str, repeat_count: int, canonical_args: str) -> str:
    return (
        "\n\n<system-reminder>\n"
        "You have repeatedly called the same tool with identical parameters many times.\n"
        "Repeated tool call detected:\n"
        f"- tool: {tool_name}\n"
        f"- repeated_times: {repeat_count}\n"
        f"- arguments: {canonical_args}\n"
        "The previous repeated calls did not make progress. Do not call this exact same tool "
        "with the exact same arguments again.\n"
        "Carefully inspect the latest tool result and choose a different next action, "
        "different parameters, or finish the task if enough evidence has been gathered."
        "\n</system-reminder>"
    )


_REMINDER_TEXT_3 = (
    "\n\n<system-reminder>\n"
    "You are stuck in a dead end and have repeatedly made the same function call without "
    "progress.\n"
    "Stop all function calls immediately. Do not call any tool in your next response.\n"
    "In analysis, review the current execution state and identify why progress is blocked.\n"
    "Then return a text-only summary to the user that reports the current problem, what has "
    "already been tried, and what information or decision is needed next."
    "\n</system-reminder>"
)

_REPEAT_REMINDER_1_START = 3
_REPEAT_REMINDER_2_START = 5
_REPEAT_REMINDER_3_START = 8
_REPEAT_FORCE_STOP_STREAK = 12

RepeatAction = Literal["none", "r1", "r2", "r3", "stop"]


def _build_repeat_reminder(
    streak: int, tool_name: str, canonical_args: str
) -> tuple[RepeatAction, str | None]:
    if streak >= _REPEAT_FORCE_STOP_STREAK:
        return "stop", _REMINDER_TEXT_3
    if streak >= _REPEAT_REMINDER_3_START:
        return "r3", _REMINDER_TEXT_3
    if streak >= _REPEAT_REMINDER_2_START:
        return "r2", _make_reminder_text_2(tool_name, streak, canonical_args)
    if streak >= _REPEAT_REMINDER_1_START:
        return "r1", _REMINDER_TEXT_1
    return "none", None


def _sort_json_value(value: object) -> object:
    if isinstance(value, list):
        return [_sort_json_value(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        value_dict = cast("dict[str, object]", value)
        return {key: _sort_json_value(value_dict[key]) for key in sorted(value_dict)}
    return value


def _canonical_tool_arguments(arguments: Any) -> str:
    try:
        return json.dumps(
            _sort_json_value(arguments),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(arguments)


def _canonical_tool_arguments_text(arguments: str) -> str:
    try:
        return _canonical_tool_arguments(json.loads(arguments, strict=False))
    except json.JSONDecodeError:
        return arguments


def _normalize_call_key(tool_name: str, arguments: str) -> ToolCallKey:
    return (tool_name, _canonical_tool_arguments_text(arguments))


def _append_reminder_to_return_value(
    return_value: Any, reminder_text: str = _REMINDER_TEXT_1
) -> Any:
    """Append dedup reminder text to a ToolReturnValue output."""
    if not isinstance(return_value, ToolReturnValue):
        return return_value

    output = return_value.output

    if isinstance(output, str):
        new_output = output + reminder_text
    else:
        new_output = list(output)
        if new_output and isinstance(new_output[-1], TextPart):
            new_output[-1] = TextPart(text=new_output[-1].text + reminder_text)
        else:
            new_output.append(TextPart(text=reminder_text))

    return return_value.model_copy(update={"output": new_output})


@dataclass(slots=True)
class MCPServerInfo:
    status: Literal["pending", "connecting", "connected", "failed", "unauthorized"]
    client: Optional[fastmcp.Client[Any]]
    tools: list[MCPTool[Any]]


class MCPTool(CallableTool, Generic[T_transport]):
    def __init__(
        self,
        server_name: str,
        mcp_tool: mcp.Tool,
        client: fastmcp.Client[T_transport],
        *,
        runtime: Any,
        **kwargs: Any,
    ):
        super().__init__(
            name=mcp_tool.name,
            description=(
                f"This is an MCP (Model Context Protocol) tool from MCP server `{server_name}`.\n\n"
                f"{mcp_tool.description or 'No description provided.'}"
            ),
            parameters=mcp_tool.inputSchema,
            **kwargs,
        )
        self._mcp_tool = mcp_tool
        self._client = client
        self._runtime = runtime
        self._timeout = timedelta(milliseconds=runtime.config.mcp.client.tool_call_timeout_ms)
        self._action_name = f"mcp:{mcp_tool.name}"

    async def __call__(self, *args: Any, **kwargs: Any) -> ToolReturnValue:
        description = f"Call MCP tool `{self._mcp_tool.name}`."
        result = await self._runtime.approval.request(self.name, self._action_name, description)
        if not result:
            return result.rejection_error()

        try:
            async with self._client as client:
                call_res = await client.call_tool(
                    self._mcp_tool.name,
                    kwargs,
                    timeout=self._timeout,
                    raise_on_error=False,
                )
                if call_res.is_error:
                    logger.warning(
                        "MCP tool returned error: {tool_name}: {content}",
                        tool_name=self._mcp_tool.name,
                        content=[str(p) for p in call_res.content][:3],
                    )
                return convert_mcp_tool_result(call_res)
        except Exception as e:
            logger.exception(
                "MCP tool execution failed: {tool_name}:", tool_name=self._mcp_tool.name
            )
            return ToolError(
                message=f"MCP tool execution failed: {e}",
                brief="MCP tool error",
            )


class WireExternalTool(CallableTool):
    def __init__(self, *, name: str, description: str, parameters: dict[str, Any]) -> None:
        super().__init__(
            name=name,
            description=description or "No description provided.",
            parameters=parameters,
        )

    async def __call__(self, *args: Any, **kwargs: Any) -> ToolReturnValue:
        tool_call = get_current_tool_call_or_none()
        if tool_call is None:
            return ToolError(
                message="External tool calls must be invoked from a tool call context.",
                brief="Invalid tool call",
            )

        from coderai.soul import get_wire_or_none

        wire = get_wire_or_none()
        if wire is None:
            logger.error(
                "Wire is not available for external tool call: {tool_name}", tool_name=self.name
            )
            return ToolError(
                message="Wire is not available for external tool calls.",
                brief="Wire unavailable",
            )

        external_tool_call = ToolCallRequest.from_tool_call(tool_call)
        wire.soul_side.send(external_tool_call)
        try:
            return await external_tool_call.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("External tool call failed: {tool_name}:", tool_name=self.name)
            return ToolError(
                message=f"External tool call failed: {e}",
                brief="External tool error",
            )


MCP_MAX_OUTPUT_CHARS = 100_000


def _media_part_size(part: ContentPart) -> int | None:
    if isinstance(part, ImageURLPart):
        return len(part.image_url.url)
    if isinstance(part, AudioURLPart):
        return len(part.audio_url.url)
    if isinstance(part, VideoURLPart):
        return len(part.video_url.url)
    return None


def convert_mcp_tool_result(result: Any) -> ToolReturnValue:
    content: list[ContentPart] = []
    char_budget = MCP_MAX_OUTPUT_CHARS
    truncated = False

    for part in result.content:
        try:
            converted = convert_mcp_content(part)
        except ValueError as exc:
            logger.warning(
                "Skipping unsupported MCP content part: {error}",
                error=exc,
            )
            converted = TextPart(text=f"[Unsupported content: {exc}]")

        if isinstance(converted, TextPart):
            if char_budget <= 0:
                truncated = True
                continue
            if len(converted.text) > char_budget:
                converted = TextPart(text=converted.text[:char_budget])
                truncated = True
            char_budget -= len(converted.text)
            content.append(converted)
            continue

        media_size = _media_part_size(converted)
        if media_size is not None:
            if media_size > char_budget:
                truncated = True
                continue
            char_budget -= media_size
            content.append(converted)
            continue

        content.append(converted)

    if truncated:
        content.append(
            TextPart(
                text=(
                    f"\n\n[Output truncated: exceeded {MCP_MAX_OUTPUT_CHARS} character limit. "
                    "Use pagination or more specific queries to get remaining content.]"
                )
            )
        )

    if result.is_error:
        return ToolError(
            output=content,
            message="Tool returned an error. The output may be error message or incomplete output",
            brief="",
        )
    else:
        return ToolOk(output=content)


__all__ = [
    "MCPServerInfo",
    "MCPTool",
    "WireExternalTool",
    "convert_mcp_tool_result",
    "set_session_id",
]
