"""Single place for routing tool-invocation names to built-in tools vs MCP.

Built-in tools live in ``ToolRegistry``. After ``mcp_connect``, the LLM also sees
dynamic functions named ``mcp__<server>__<tool>`` (see ``mcp_client.get_tools_as_openai_format``).

``<tool>`` may contain ``__``; ``<server>`` must not (validated at connect time).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

MCP_FUNCTION_PREFIX = "mcp__"
MAX_PROVIDER_FUNCTION_NAME_LENGTH = 64
_PROVIDER_FUNCTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def build_mcp_function_name(server: str, tool: str) -> str:
    """Build an exact provider-safe MCP function name or raise ``ValueError``."""
    if not server or "__" in server or not _PROVIDER_FUNCTION_NAME_RE.fullmatch(server):
        raise ValueError(
            "MCP server names must contain only letters, numbers, '_' or '-', "
            "and must not contain '__'"
        )
    if not tool or not _PROVIDER_FUNCTION_NAME_RE.fullmatch(tool):
        raise ValueError("MCP tool names must contain only letters, numbers, '_' or '-'")
    name = f"{MCP_FUNCTION_PREFIX}{server}__{tool}"
    if len(name) > MAX_PROVIDER_FUNCTION_NAME_LENGTH:
        raise ValueError(
            f"MCP function name {name!r} is {len(name)} characters; provider limit is "
            f"{MAX_PROVIDER_FUNCTION_NAME_LENGTH}"
        )
    return name


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
    if (
        not name
        or len(name) > MAX_PROVIDER_FUNCTION_NAME_LENGTH
        or not _PROVIDER_FUNCTION_NAME_RE.fullmatch(name)
        or not name.startswith(MCP_FUNCTION_PREFIX)
    ):
        return None
    body = name[len(MCP_FUNCTION_PREFIX) :]
    idx = body.find("__")
    if idx <= 0 or idx >= len(body) - 2:
        return None
    server = body[:idx]
    tool = body[idx + 2 :]
    if not server or not tool:
        return None
    try:
        build_mcp_function_name(server, tool)
    except ValueError:
        return None
    return (server, tool)


def is_mcp_function_name(name: str) -> bool:
    """True if *name* is a valid MCP-prefixed function id."""
    return parse_mcp_function_name(name) is not None


async def call_mcp_tool_by_function_name(
    name: str, arguments: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Dispatch ``mcp__...`` tool calls to :func:`mcp_client.call_tool`."""
    from coderAI.core.services import get_services

    parsed = parse_mcp_function_name(name)
    if not parsed:
        return {
            "success": False,
            "error": f"Invalid MCP tool name {name!r}. Expected mcp__<server>__<tool>.",
        }
    server, tool_name = parsed
    mcp_client = get_services().mcp_client
    discovered = any(
        item.get("server") == server and item.get("name") == tool_name
        for item in mcp_client.discovered_tools
    )
    server_info = mcp_client.servers.get(server)
    if not discovered or server_info is None or server_info.get("degraded"):
        return {
            "success": False,
            "error": f"MCP tool {name!r} is not currently advertised by a healthy server.",
        }
    return await mcp_client.call_tool(server, tool_name, arguments or {})
