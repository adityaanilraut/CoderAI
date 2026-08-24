"""Built-in tool surface — type-safe registry, execution engine + dynamic MCP."""

from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.registry import ToolRegistry, get_tool_registry
from coderai.core.tools.schema import (
    assert_supported_json_schema,
    define_tool,
    validate_json_schema_value,
)
from coderai.core.tools.types import (
    BackgroundProcessCompletion,
    ProcessTimeoutControl,
    ProcessTimeoutInfo,
    ToolCall,
    ToolCategory,
    ToolDefinition,
    ToolError,
    ToolExecutionContext,
    ToolExecutionFollowUpMessage,
    ToolExecutionHooks,
    ToolResult,
    ValidationError,
    normalize_tool_call,
)

__all__ = [
    "BackgroundProcessCompletion",
    "ProcessTimeoutControl",
    "ProcessTimeoutInfo",
    "ToolCall",
    "ToolCategory",
    "ToolDefinition",
    "ToolError",
    "ToolExecutionContext",
    "ToolExecutionFollowUpMessage",
    "ToolExecutionHooks",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ValidationError",
    "assert_supported_json_schema",
    "define_tool",
    "get_tool_registry",
    "normalize_tool_call",
    "validate_json_schema_value",
]
