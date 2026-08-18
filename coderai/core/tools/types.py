"""Shared tool types — port of deepcode core/src/common/tool-types.ts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from collections.abc import Callable

PluginRateLimitedTool = Literal["UnderstandImage", "WebSearch", "WebFetch"]
ToolCategory = Literal["filesystem", "shell", "web", "interactive", "meta", "mcp", "subagent"]


class ValidationError(Exception):
    """Raised when tool arguments fail schema or type validation."""


class ToolExecutionError(Exception):
    """Raised when an unhandled execution error occurs in a tool handler."""


@dataclass
class ToolExecutionFollowUpMessage:
    role: str = "system"
    content: str = ""
    content_params: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.content_params is not None:
            d["contentParams"] = self.content_params
        return d


@dataclass
class ToolResult:
    ok: bool
    name: str
    output: str | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None
    await_user_response: bool = False
    follow_up_messages: list[ToolExecutionFollowUpMessage | dict[str, Any]] = field(
        default_factory=list
    )


# Alias for compatibility with deepcode naming
ToolExecutionResult = ToolResult


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
    bash_timeout_ms: int | None = None
    bash_min_timeout_ms: int | None = None

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
    should_stop: Callable[[], bool] | None = None


@dataclass
class ToolCallExecution:
    tool_call_id: str
    content: str
    result: ToolResult


@dataclass
class ToolDefinition:
    """Type-safe specification and metadata for a registered tool."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    handler: Callable[..., Any] | None = None
    aliases: list[str] = field(default_factory=list)
    category: ToolCategory = "meta"
    rate_limited_id: PluginRateLimitedTool | None = None
    is_mutating: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """Generate OpenAI function calling JSON schema definition."""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": self.parameters,
        }
        if self.required:
            schema["required"] = self.required
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
