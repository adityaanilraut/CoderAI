""""""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from collections.abc import Callable

PluginRateLimitedTool = Literal["UnderstandImage", "WebSearch", "WebFetch"]
ToolCategory = Literal["filesystem", "shell", "web", "interactive", "meta", "mcp", "subagent"]


# Canonical Tool Exceptions
class ToolError(Exception):
    """Base exception for all tool subsystem errors."""

    def __init__(self, message: str, code: str = "TOOL_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ValidationError(ToolError):
    """Raised when tool arguments fail schema or type validation."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INVALID_TOOL_ARGUMENTS")


DISALLOWED_OPENAI_SCHEMA_KEYS = frozenset({"uniqueItems", "$schema", "$id"})


def canonicalize_tool_schema(schema: Any) -> Any:
    """Recursively canonicalize and sort dictionary keys in a tool schema for deterministic serialization."""
    if isinstance(schema, dict):
        return {
            k: canonicalize_tool_schema(v)
            for k, v in sorted(schema.items())
            if k not in DISALLOWED_OPENAI_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [canonicalize_tool_schema(elem) for elem in schema]
    return schema


@dataclass
class ToolExecutionFollowUpMessage:
    role: str = "user"
    content: str = ""
    content_params: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.content_params is not None:
            d["contentParams"] = self.content_params
        return d


@dataclass
class ToolResult:
    """The discriminated outcome of a tool execution."""

    ok: bool
    name: str
    output: str | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None
    await_user_response: bool = False
    follow_up_messages: list[ToolExecutionFollowUpMessage | dict[str, Any]] = field(
        default_factory=list
    )
    concludes_turn: bool = False


@dataclass
class ProcessTimeoutInfo:
    timeout_ms: int
    started_at_ms: int
    deadline_at_ms: int
    timed_out: bool


@dataclass
class ProcessTimeoutControl:
    get_info: Callable[[], ProcessTimeoutInfo]
    set_timeout_ms: Callable[[int], ProcessTimeoutInfo]


@dataclass
class BackgroundProcessCompletion:
    task_id: str
    process_id: int
    command: str
    output_path: str
    ok: bool
    exit_code: int | None
    signal: str | None
    started_at_ms: int
    completed_at_ms: int
    error: str | None = None
    cwd: str | None = None
    shell_path: str = ""


@dataclass
class ToolExecutionContext:
    """Runtime context handed to a tool implementation with deferral and lifecycle hooks."""

    session_id: str
    project_root: str
    tool_call: dict[str, Any] = field(default_factory=dict)
    create_openai_client: Callable[[], dict[str, Any]] | None = None
    on_process_start: Callable[[str | int, str], None] | None = None
    on_process_exit: Callable[[str | int], None] | None = None
    on_process_stdout: Callable[[str | int, str], None] | None = None
    on_process_timeout_control: Callable[[str | int, ProcessTimeoutControl | None], None] | None = (
        None
    )
    on_background_process_complete: Callable[[BackgroundProcessCompletion], None] | None = None
    on_before_file_mutation: Callable[[str], None] | None = None
    on_after_file_mutation: Callable[[str], None] | None = None
    on_plugin_rate_limit_exceeded: Callable[[str], None] | None = None
    on_load_skill: Callable[[str], Any] | None = None
    bash_timeout_ms: int | None = None
    bash_min_timeout_ms: int | None = None
    permission_decision: str | None = None
    sandbox_mode: str | None = None
    list_session_messages: Callable[[str], list[Any]] | None = None
    list_session_events: Callable[[str], list[Any]] | None = None
    deferred_contexts: list[ToolExecutionFollowUpMessage | dict[str, Any]] = field(
        default_factory=list
    )
    is_turn_concluded: bool = False

    def defer_context(self, message: ToolExecutionFollowUpMessage | dict[str, Any]) -> None:
        """Attach a follow-up context message to this execution result."""
        self.deferred_contexts.append(message)

    def conclude_turn(self) -> None:
        """Mark the current agent turn complete after this result."""
        self.is_turn_concluded = True

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like access for backwards compatibility."""
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass
class ToolExecutionHooks:
    on_process_start: Callable[[str | int, str], None] | None = None
    on_process_exit: Callable[[str | int], None] | None = None
    on_process_stdout: Callable[[str | int, str], None] | None = None
    on_process_timeout_control: Callable[[str | int, ProcessTimeoutControl | None], None] | None = (
        None
    )
    on_background_process_complete: Callable[[BackgroundProcessCompletion], None] | None = None
    on_before_file_mutation: Callable[[str], None] | None = None
    on_after_file_mutation: Callable[[str], None] | None = None
    on_plugin_rate_limit_exceeded: Callable[[str], None] | None = None
    on_load_skill: Callable[[str], Any] | None = None
    should_stop: Callable[[], bool] | None = None
    permission_decision: str | None = None
    sandbox_mode: str | None = None
    pre_execute: Callable[[str, dict[str, Any], "ToolExecutionContext"], str] | None = None
    post_execute: (
        Callable[[str, dict[str, Any], ToolResult, "ToolExecutionContext"], ToolResult | None]
        | None
    ) = None
    guards: list[Callable[[str, dict[str, Any], "ToolExecutionContext"], str]] = field(
        default_factory=list
    )
    timeout_ms: int | None = None
    list_session_messages: Callable[[str], list[Any]] | None = None
    list_session_events: Callable[[str], list[Any]] | None = None


@dataclass
class ToolDefinition:
    """Type-safe specification and execution contract for a registered tool."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    handler: Callable[..., Any] | None = None
    aliases: list[str] = field(default_factory=list)
    category: ToolCategory = "meta"
    rate_limited_id: PluginRateLimitedTool | None = None
    is_mutating: bool = False
    is_concurrency_safe: bool | Callable[[dict[str, Any]], bool] = False
    timeout_ms: int | None = None
    present_result: Callable[[dict[str, Any], ToolResult], dict[str, Any] | None] | None = None
    finalize_content: Callable[[ToolExecutionContext, ToolResult], str | None] | None = None

    def check_concurrency_safe(self, args: dict[str, Any]) -> bool:
        """Evaluate whether this invocation can run concurrently in parallel groups."""
        if callable(self.is_concurrency_safe):
            try:
                return bool(self.is_concurrency_safe(args))
            except Exception:
                return False
        return bool(self.is_concurrency_safe)

    def to_openai_schema(self) -> dict[str, Any]:
        """Generate OpenAI function calling JSON schema definition with deterministic key sorting."""
        props = canonicalize_tool_schema(self.parameters)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": props,
        }
        if self.required:
            schema["required"] = sorted(self.required)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


def as_str(value: Any, default: str = "") -> str:
    """Extract a string from a tool argument, defaulting otherwise."""
    return value if isinstance(value, str) else default


# A tool call as produced by the LLM (OpenAI format).
ToolCall = dict[str, Any]


def normalize_tool_call(raw: Any) -> ToolCall | None:
    """Normalize SDK objects and dictionaries to one OpenAI tool-call shape."""
    if isinstance(raw, dict):
        tool_call_id = raw.get("id")
        function = raw.get("function")
        if not isinstance(function, dict):
            return None
        name = function.get("name")
        arguments = function.get("arguments", "")
    else:
        tool_call_id = getattr(raw, "id", None)
        function = getattr(raw, "function", None)
        name = getattr(function, "name", None)
        arguments = getattr(function, "arguments", "")

    if not isinstance(tool_call_id, str) or not isinstance(name, str):
        return None
    return {
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else "",
        },
    }
