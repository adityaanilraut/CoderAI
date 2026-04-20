"""Single place for routing tool-invocation names to built-in tools vs MCP.

Built-in tools live in ``ToolRegistry``. After ``mcp_connect``, the LLM also sees
dynamic functions named ``mcp__<server>__<tool>`` (see ``mcp_client.get_tools_as_openai_format``).

``<tool>`` may contain ``__``; ``<server>`` must not (validated at connect time).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

MCP_FUNCTION_PREFIX = "mcp__"

def coerce_tool_arguments(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse tool ``function.arguments`` from providers (string JSON or dict)."""
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, f"Tool arguments must be JSON object or string, got {type(raw).__name__}"
    s = raw.strip()
    if not s:
        return {}, None
    try:
        out = json.loads(s)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in tool arguments: {e}"
    if not isinstance(out, dict):
        return None, f"Tool arguments must decode to a JSON object, got {type(out).__name__}"
    return out, None


def parse_mcp_function_name(name: str) -> Optional[Tuple[str, str]]:
    """Parse ``mcp__<server>__<tool>``.

    The tool segment is everything after the *first* ``__`` following the server
    segment, so tool names may include ``__``.
    """
    if not name or not name.startswith(MCP_FUNCTION_PREFIX):
        return None
    body = name[len(MCP_FUNCTION_PREFIX) :]
    idx = body.find("__")
    if idx <= 0 or idx >= len(body) - 2:
        return None
    server = body[:idx]
    tool = body[idx + 2 :]
    if not server.strip() or not tool.strip():
        return None
    return (server, tool)


def is_mcp_function_name(name: str) -> bool:
    """True if *name* is a valid MCP-prefixed function id."""
    return parse_mcp_function_name(name) is not None


async def call_mcp_tool_by_function_name(
    name: str, arguments: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Dispatch ``mcp__...`` tool calls to :func:`mcp_client.call_tool`."""
    from .tools.mcp import mcp_client

    parsed = parse_mcp_function_name(name)
    if not parsed:
        return {
            "success": False,
            "error": f"Invalid MCP tool name {name!r}. Expected mcp__<server>__<tool>.",
        }
    server, tool_name = parsed
    return await mcp_client.call_tool(server, tool_name, arguments or {})
