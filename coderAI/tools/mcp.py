"""MCP (Model Context Protocol) client for connecting to external MCP servers."""

import atexit
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)


class MCPClient:
    """Client for connecting to MCP servers and discovering tools.

    Supports stdio transport for connecting to MCP-compatible servers.
    Discovered tools are registered in the CoderAI tool registry.
    """

    def __init__(self):
        """Initialize MCP client."""
        self.servers: Dict[str, Dict[str, Any]] = {}
        self.discovered_tools: List[Dict[str, Any]] = []
        self._next_id: int = 1

    def _get_next_id(self) -> int:
        """Return a unique, incrementing JSON-RPC request ID."""
        current = self._next_id
        self._next_id += 1
        return current

    async def _read_response(
        self,
        stdout: asyncio.StreamReader,
        expected_id: int,
        timeout: float = 10,
    ) -> Dict[str, Any]:
        """Read lines from stdout until a JSON-RPC response with the expected id arrives.

        Skips any notifications (messages without an 'id' field) that servers
        may send between requests.
        """
        import time

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            line = await asyncio.wait_for(stdout.readline(), timeout=remaining)
            if not line:
                raise RuntimeError("Server closed stdout unexpectedly")
            parsed = json.loads(line.decode())
            # Skip notifications (no 'id' field)
            if "id" not in parsed:
                continue
            if parsed["id"] == expected_id:
                return parsed
            # Unexpected id — log and keep reading
            logger.warning(f"Unexpected JSON-RPC id {parsed.get('id')}, expected {expected_id}")

    async def connect_stdio(
        self, server_name: str, command: str, args: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Connect to an MCP server via stdio transport.

        Args:
            server_name: Friendly name for this server connection
            command: Server command to run (e.g., 'npx', 'python3')
            args: Command line arguments for the server

        Returns:
            Connection result with discovered tools
        """
        process = None
        try:
            full_args = [command] + (args or [])
            process = await asyncio.create_subprocess_exec(
                *full_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Send MCP initialize request (JSON-RPC 2.0)
            init_id = self._get_next_id()
            init_request = {
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "CoderAI",
                        "version": "0.1.0",
                    },
                },
            }

            process.stdin.write((json.dumps(init_request) + "\n").encode())
            await process.stdin.drain()

            # Read response with timeout (skips any interleaved notifications)
            try:
                init_response = await self._read_response(
                    process.stdout, init_id, timeout=10
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' did not respond to initialize within 10s",
                }

            # Send initialized notification
            initialized_notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            process.stdin.write((json.dumps(initialized_notif) + "\n").encode())
            await process.stdin.drain()

            # Request tool list
            tools_id = self._get_next_id()
            tools_request = {
                "jsonrpc": "2.0",
                "id": tools_id,
                "method": "tools/list",
            }
            process.stdin.write((json.dumps(tools_request) + "\n").encode())
            await process.stdin.drain()

            try:
                tools_response = await self._read_response(
                    process.stdout, tools_id, timeout=10
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' did not respond to tools/list",
                }

            # Store connection info
            server_info = tools_response.get("result", {}).get("tools", [])
            self.servers[server_name] = {
                "process": process,
                "tools": server_info,
                "server_info": init_response.get("result", {}),
            }

            # Store discovered tools
            for tool in server_info:
                self.discovered_tools.append({
                    "server": server_name,
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("inputSchema", {}),
                })

            return {
                "success": True,
                "server": server_name,
                "tools_discovered": len(server_info),
                "tools": [t.get("name") for t in server_info],
                "server_info": init_response.get("result", {}).get("serverInfo", {}),
            }

        except FileNotFoundError:
            return {
                "success": False,
                "error": f"Command not found: {command}. Is the MCP server installed?",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            # Kill the process if it was spawned but not successfully stored
            if process is not None and server_name not in self.servers:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call a tool on a connected MCP server.

        Args:
            server_name: Name of the connected server
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool execution result
        """
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}

        server = self.servers[server_name]
        process = server["process"]

        if process.returncode is not None:
            return {"success": False, "error": f"Server '{server_name}' process has exited"}

        try:
            call_id = self._get_next_id()
            request = {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            }

            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()

            try:
                response = await self._read_response(
                    process.stdout, call_id, timeout=30
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Tool call '{tool_name}' timed out after 30s",
                }

            result = response.get("result", {})
            error = response.get("error")

            if error:
                return {
                    "success": False,
                    "error": error.get("message", str(error)),
                }

            # Extract text content from MCP response
            content_parts = result.get("content", [])
            text_content = ""
            for part in content_parts:
                if part.get("type") == "text":
                    text_content += part.get("text", "")

            return {
                "success": True,
                "content": text_content,
                "raw": result,
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def disconnect(self, server_name: str) -> Dict[str, Any]:
        """Disconnect from an MCP server.

        Args:
            server_name: Name of the server to disconnect from

        Returns:
            Result dictionary
        """
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}

        try:
            process = self.servers[server_name]["process"]
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()

        del self.servers[server_name]
        self.discovered_tools = [
            t for t in self.discovered_tools if t.get("server") != server_name
        ]

        return {"success": True, "message": f"Disconnected from {server_name}"}

    async def disconnect_all(self):
        """Disconnect from all MCP servers."""
        for name in list(self.servers.keys()):
            await self.disconnect(name)

    def get_tools_as_openai_format(self) -> List[Dict[str, Any]]:
        """Get discovered MCP tools in OpenAI function-calling format.

        Returns:
            List of tool definitions compatible with OpenAI's API
        """
        tools = []
        for tool in self.discovered_tools:
            tools.append({
                "type": "function",
                "function": {
                    "name": f"mcp_{tool['server']}_{tool['name']}",
                    "description": f"[MCP: {tool['server']}] {tool['description']}",
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return tools


# Global MCP client instance
mcp_client = MCPClient()

def _cleanup_mcp_servers():
    """Synchronous cleanup of MCP servers on exit."""
    import time
    for name, info in list(mcp_client.servers.items()):
        try:
            proc = info["process"]
            if proc.returncode is None:
                proc.kill()
        except Exception:
            pass
    mcp_client.servers.clear()

atexit.register(_cleanup_mcp_servers)


class MCPConnectParams(BaseModel):
    server_name: str = Field(..., description="Friendly name for this server connection")
    command: str = Field(..., description="Command to start the MCP server (e.g., 'npx')")
    args: Optional[List[str]] = Field(None, description="Arguments for the server command")


class MCPConnectTool(Tool):
    """Tool for connecting to MCP servers."""

    name = "mcp_connect"
    description = "Connect to an MCP (Model Context Protocol) server to discover and use its tools"
    parameters_model = MCPConnectParams

    async def execute(
        self, server_name: str, command: str, args: list = None
    ) -> Dict[str, Any]:
        """Connect to an MCP server."""
        return await mcp_client.connect_stdio(server_name, command, args)


class MCPCallToolParams(BaseModel):
    server_name: str = Field(..., description="Name of the connected MCP server")
    tool_name: str = Field(..., description="Name of the tool to call on the server")
    arguments: Optional[Dict[str, Any]] = Field(None, description="Arguments to pass to the tool")


class MCPCallTool(Tool):
    """Tool for calling tools on connected MCP servers."""

    name = "mcp_call_tool"
    description = "Call a tool on a connected MCP server"
    parameters_model = MCPCallToolParams

    async def execute(
        self, server_name: str, tool_name: str, arguments: dict = None
    ) -> Dict[str, Any]:
        """Call a tool on a connected MCP server."""
        return await mcp_client.call_tool(server_name, tool_name, arguments or {})


class MCPListParams(BaseModel):
    pass


class MCPListTool(Tool):
    """Tool for listing connected MCP servers and their tools."""

    name = "mcp_list"
    description = "List all connected MCP servers and discovered tools"
    parameters_model = MCPListParams
    is_read_only = True

    async def execute(self) -> Dict[str, Any]:
        """List MCP servers and tools."""
        servers = {}
        for name, info in mcp_client.servers.items():
            servers[name] = {
                "tools": [t.get("name") for t in info.get("tools", [])],
                "server_info": info.get("server_info", {}),
            }

        return {
            "success": True,
            "connected_servers": len(servers),
            "servers": servers,
            "total_tools": len(mcp_client.discovered_tools),
        }
