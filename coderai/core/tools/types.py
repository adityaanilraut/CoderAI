"""Shared tool types — port of deepcode core/src/common/tool-types.ts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from collections.abc import Callable

PluginRateLimitedTool = Literal["UnderstandImage", "WebSearch"]


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


def as_str(value: Any, default: str = "") -> str:
    """Extract a string from a tool argument, defaulting otherwise."""
    return value if isinstance(value, str) else default


# A tool call as produced by the LLM (OpenAI format).
ToolCall = dict[str, Any]
