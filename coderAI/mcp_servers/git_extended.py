"""Stdio MCP server exposing rarely used git tools.

Run as::

    python -m coderAI.mcp_servers.git_extended

CoderAI auto-registers this under the server name ``git_extended`` so agents
see tools as ``mcp__git_extended__git_push``, etc. Everyday git ops
(``git_status``, ``git_diff``, ``git_add``, ``git_commit``, ``git_log``,
``git_branch``) remain native tools.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Dict, List, Optional

from coderAI.tools.git_extended import EXTENDED_GIT_TOOLS


SERVER_NAME = "git_extended"
PROTOCOL_VERSION = "2024-11-05"


def _tool_descriptors() -> List[Dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.get_parameters(),
        }
        for tool in EXTENDED_GIT_TOOLS
    ]


def _tools_by_name() -> Dict[str, Any]:
    return {t.name: t for t in EXTENDED_GIT_TOOLS}


def _ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _write(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


async def _call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    tool = _tools_by_name().get(name)
    if tool is None:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }
    try:
        result = await tool.execute(**(arguments or {}))
    except TypeError as e:
        return {
            "content": [{"type": "text", "text": f"Invalid arguments: {e}"}],
            "isError": True,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": str(e)}],
            "isError": True,
        }

    is_error = not bool(result.get("success", True))
    return {
        "content": [{"type": "text", "text": json.dumps(result, default=str)}],
        "isError": is_error,
    }


async def _handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        return None

    if method == "initialize":
        return _ok(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            },
        )

    if method == "tools/list":
        return _ok(req_id, {"tools": _tool_descriptors()})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _err(req_id, -32602, "arguments must be an object")
        return _ok(req_id, await _call_tool(name, arguments))

    if method == "ping":
        return _ok(req_id, {})

    return _err(req_id, -32601, f"Method not found: {method}")


async def _serve() -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        reply = await _handle(msg)
        if reply is not None:
            _write(reply)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
