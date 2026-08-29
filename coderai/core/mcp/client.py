"""JSON-RPC 2.0 MCP client supporting stdio and SSE transports."""

from __future__ import annotations

import asyncio
from typing import Any
from collections.abc import Callable

from coderai.core.mcp.transport import (
    McpTransport,
    SseMcpTransport,
    StdioMcpTransport,
    StreamableHttpMcpTransport,
    create_mcp_spawn_spec,
)

__all__ = ["McpClient", "create_mcp_spawn_spec"]


class McpClient:
    """Client for Model Context Protocol servers over stdio or SSE transports."""

    def __init__(
        self,
        server_name: str,
        command_or_config: str | dict[str, Any],
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.server_name = server_name
        self.config: dict[str, Any] = (
            command_or_config
            if isinstance(command_or_config, dict)
            else {"command": command_or_config}
        )
        if not isinstance(command_or_config, dict):
            if args:
                self.config["args"] = args
            if env:
                self.config["env"] = env
            if cwd:
                self.config["cwd"] = cwd

        # Initialize transport based on config
        self.transport: McpTransport = self._create_transport()

        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tools: list[dict[str, Any]] = []
        self._prompts: list[dict[str, Any]] = []
        self._resources: list[dict[str, Any]] = []
        self._disconnected = False
        self._disconnect_handler: Callable[[str], None] | None = None
        self._notification_handler: Callable[[str, dict[str, Any]], None] | None = None
        self.server_capabilities: dict[str, Any] = {}

    def _create_transport(self) -> McpTransport:
        if "url" in self.config:
            from coderai.core.network.security import NetworkPolicy

            policy = self.config.get("policy")
            if policy is None and self.config.get("allowPrivateIps"):
                policy = NetworkPolicy(allow_private_ips=True)
            transport = str(
                self.config.get("transport") or self.config.get("type") or "sse"
            ).lower()
            if transport in ("http", "streamable-http", "streamable_http"):
                return StreamableHttpMcpTransport(
                    server_name=self.server_name,
                    url=self.config["url"],
                    headers=self.config.get("headers"),
                    policy=policy,
                )
            return SseMcpTransport(
                server_name=self.server_name,
                url=self.config["url"],
                headers=self.config.get("headers"),
                policy=policy,
            )
        return StdioMcpTransport(
            server_name=self.server_name,
            command=self.config.get("command", ""),
            args=self.config.get("args"),
            env=self.config.get("env"),
            cwd=self.config.get("cwd"),
        )

    def is_connected(self) -> bool:
        return self.transport.is_connected() and not self._disconnected

    def set_on_disconnect(self, handler: Callable[[str], None]) -> None:
        self._disconnect_handler = handler

    def set_notification_handler(self, handler: Callable[[str, dict[str, Any]], None]) -> None:
        self._notification_handler = handler

    def _on_transport_message(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return

        msg_id = message.get("id")
        if isinstance(msg_id, int):
            future = self._pending.get(msg_id)
            if future and not future.done():
                loop = future.get_loop()
                if "error" in message:
                    err_obj = message["error"]
                    err_msg = (
                        err_obj.get("message", "MCP error")
                        if isinstance(err_obj, dict)
                        else str(err_obj)
                    )
                    loop.call_soon_threadsafe(future.set_exception, RuntimeError(err_msg))
                else:
                    loop.call_soon_threadsafe(future.set_result, message.get("result"))
        elif "method" in message:
            # JSON-RPC Notification from server
            method = message.get("method", "")
            params = message.get("params") or {}
            if self._notification_handler:
                self._notification_handler(method, params)

    def _on_transport_disconnect(self, reason: str) -> None:
        self._disconnected = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server '{self.server_name}' disconnected."))
        self._pending.clear()
        if self._disconnect_handler:
            self._disconnect_handler(reason)

    async def connect(self, timeout_s: float = 30.0) -> None:
        self.transport.set_handlers(
            on_message=self._on_transport_message,
            on_disconnect=self._on_transport_disconnect,
        )

        try:
            await self.transport.connect(timeout_s=timeout_s)
            self._disconnected = False

            # Protocol Handshake: initialize
            init_res = await asyncio.wait_for(
                self._request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "roots": {"listChanged": True},
                            "sampling": {},
                        },
                        "clientInfo": {"name": "coderai", "version": "0.0.0"},
                    },
                    timeout_s=timeout_s,
                ),
                timeout=timeout_s,
            )
            if isinstance(init_res, dict):
                self.server_capabilities = init_res.get("capabilities") or {}

            # Notification: initialized
            await self._notify("notifications/initialized", {})

            # Fetch initial tools list
            tools_res = await self.list_tools(timeout_s=timeout_s)
            self._tools = tools_res
        except Exception as e:
            await self.disconnect()
            raise RuntimeError(f"Failed to connect to MCP server '{self.server_name}': {e}") from e

    async def disconnect(self) -> None:
        self._disconnected = True
        await self.transport.disconnect()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server '{self.server_name}' disconnected."))
        self._pending.clear()

    async def ping(self, timeout_s: float = 5.0) -> bool:
        """Probe MCP connection liveness by sending a ping request."""
        if not self.is_connected():
            return False
        try:
            await self._request("ping", {}, timeout_s=timeout_s)
            return True
        except Exception:
            return False

    async def list_tools(
        self, cursor: str | None = None, timeout_s: float = 30.0
    ) -> list[dict[str, Any]]:

        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        result = await self._request("tools/list", params, timeout_s=timeout_s)
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        self._tools = tools
        return tools

    async def call_tool(
        self, name: str, args: dict[str, Any], timeout_s: float = 60.0
    ) -> dict[str, Any]:
        result = await self._request(
            "tools/call", {"name": name, "arguments": args}, timeout_s=timeout_s
        )
        if isinstance(result, dict):
            return result
        return {"content": [{"type": "text", "text": str(result)}], "isError": False}

    async def list_prompts(
        self, cursor: str | None = None, timeout_s: float = 30.0
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        result = await self._request("prompts/list", params, timeout_s=timeout_s)
        prompts = (result or {}).get("prompts", []) if isinstance(result, dict) else []
        self._prompts = prompts
        return prompts

    async def get_prompt(
        self, name: str, args: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        result = await self._request(
            "prompts/get", {"name": name, "arguments": args}, timeout_s=timeout_s
        )
        return result if isinstance(result, dict) else {"messages": []}

    async def list_resources(
        self, cursor: str | None = None, timeout_s: float = 30.0
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        result = await self._request("resources/list", params, timeout_s=timeout_s)
        resources = (result or {}).get("resources", []) if isinstance(result, dict) else []
        self._resources = resources
        return resources

    async def read_resource(self, uri: str, timeout_s: float = 30.0) -> dict[str, Any]:
        result = await self._request("resources/read", {"uri": uri}, timeout_s=timeout_s)
        return result if isinstance(result, dict) else {"contents": []}

    async def _request(self, method: str, params: dict[str, Any], timeout_s: float = 30.0) -> Any:
        if self._disconnected or not self.transport.is_connected():
            raise RuntimeError(f"MCP server '{self.server_name}' is not connected.")

        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future

        self.transport.send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            self._pending.pop(msg_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        self.transport.send({"jsonrpc": "2.0", "method": method, "params": params})
