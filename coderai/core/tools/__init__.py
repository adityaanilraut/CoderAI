"""Built-in tool surface — type-safe registry, execution engine + dynamic MCP."""

from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.registry import ToolRegistry, get_tool_registry
from coderai.core.tools.types import (
    BackgroundProcessCompletion,
    ProcessTimeoutControl,
    ProcessTimeoutInfo,
    ToolCall,
    ToolCallExecution,
    ToolCategory,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
    ToolExecutionFollowUpMessage,
    ToolExecutionHooks,
    ToolExecutionResult,
    ToolResult,
    ValidationError,
)

__all__ = [
    "BackgroundProcessCompletion",
    "ProcessTimeoutControl",
    "ProcessTimeoutInfo",
    "ToolCall",
    "ToolCallExecution",
    "ToolCategory",
    "ToolDefinition",
    "ToolExecutionContext",
    "ToolExecutionError",
    "ToolExecutionFollowUpMessage",
    "ToolExecutionHooks",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ValidationError",
    "get_tool_registry",
]
